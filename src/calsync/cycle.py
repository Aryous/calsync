"""One sync cycle as `calsync run` and the daemon run it: the engine's cycle plus the rules
whose account could not be set up, recorded as failed runs so the history stays complete."""

from collections.abc import Sequence

from calsync.config import Config
from calsync.engine import CycleResult, Engine, RuleResult
from calsync.store import RunRecord, Store
from calsync.wiring import Adapters


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
    selected = [r for r in config.rules if rules is None or r.name in rules]
    skipped: dict[str, RuleResult] = {}
    for rule in selected:
        broken = [a for a in (rule.source.account, rule.target.account) if a in adapters.errors]
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
