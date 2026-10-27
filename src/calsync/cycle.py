"""One sync cycle as `calsync run` and the daemon run it: the engine's cycle plus the rules
whose account could not be set up, recorded as failed runs so the history stays complete.
Paused rules are skipped; a retiring rule whose copies are all gone is removed afterwards
(``retired`` / ``remove_retired``)."""

import logging
from collections.abc import Sequence

from calsync import debug, ops
from calsync.config import Config
from calsync.engine import CycleResult, Engine, RuleResult
from calsync.naming import rule_tag
from calsync.store import RunRecord, Store
from calsync.wiring import Adapters

log = logging.getLogger(__name__)


async def run_cycle(
    engine: Engine,
    store: Store,
    config: Config,
    adapters: Adapters,
    rules: Sequence[str] | None,
    *,
    dry_run: bool,
    allow_deletes: bool,
) -> CycleResult:
    """Run ``rules`` (None: every rule) and return their results in config order.

    A rule whose source or target account has a setup error (``adapters.errors``) fails on its
    own without reaching the engine; the other rules run normally.
    """
    selected = [r for r in config.rules if (rules is None or r.name in rules) and not r.paused]
    skipped: dict[str, RuleResult] = {}
    for rule in selected:
        sides = (rule.target.account,) if rule.retire else (rule.source.account, rule.target.account)
        broken = [a for a in sides if a in adapters.errors]
        if broken:
            skipped[rule.name] = RuleResult(rule.name)
            skipped[rule.name].fail(adapters.errors[broken[0]])
    result = await engine.cycle(
        rules=[r.name for r in selected if r.name not in skipped],
        dry_run=dry_run,
        allow_deletes=allow_deletes,
    )
    if skipped and not dry_run:  # keep run history complete
        start, end = result.started_at, result.finished_at
        store.record_runs(
            [
                RunRecord(
                    r.rule,
                    start,
                    end,
                    r.status,
                    0,
                    0,
                    0,
                    0,
                    result.requests,
                    r.error,
                    error_kind=r.error_kind,
                )
                for r in skipped.values()
            ]
        )
    result.rules = {r.name: result.rules.get(r.name) or skipped[r.name] for r in selected}
    return result


def counts_line(name: str, result: RuleResult) -> str:
    """A rule's outcome as counts: ``rule=… status=… created=…`` (never a title or an error)."""
    line = (
        f"rule={name} status={result.status} created={result.created} updated={result.updated} "
        f"deleted={result.deleted} failed={result.failed}"
    )
    for label in ("protected", "degraded", "out_of_window", "reverted"):
        if value := getattr(result, label):
            line += f" {label}={value}"
    return line


def retired(config: Config, result: CycleResult, store: Store) -> list[str]:
    """The retiring rules of ``config`` that ``result`` finished: the rule ended ``ok`` against
    the target ``config`` still names, and either that calendar does not exist or the cycle's
    detection read every changed copy and the store records no copy of the rule there. Anything
    less (an unread copy, a target edited since) waits for the next cycle."""
    by_name = {r.name: r for r in config.rules}
    return [
        name
        for name, r in result.rules.items()
        if (rule := by_name.get(name)) is not None
        and rule.retire
        and r.status == "ok"
        and r.target == (rule.target.account, rule.target.calendar)
        and (
            r.target_missing
            or (
                r.detection_complete
                and store.copies(rule_tag(name), rule.target.account, rule.target.calendar) == 0
            )
        )
    ]


def remove_retired(ctx: ops.OpContext, name: str) -> ops.OpResult | None:
    """Remove a rule that retired (``rule.remove`` with ``after_retire``, an argument only
    calsync itself passes). A failure (a conflict with a hand edit, an unreadable file) is logged
    by type; the next cycle that finds the rule retired tries again. None on failure."""
    try:
        return ops.run_op(ctx, "rule.remove", {"name": name, "after_retire": True}, False)
    except Exception as exc:  # never the text: a config problem can quote a value
        log.warning("rule %s retired, but removing it from the config failed (%s)", name, type(exc).__name__)
        debug.details(log, f"rule {name}: removing it from the config failed", exc)
        return None
