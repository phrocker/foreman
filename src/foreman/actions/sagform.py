"""Actions as SAG statements.

Foreman needs a mechanical answer to "is this the same action I approved
before". Hashing a serialized dict would work, but it invents a canonical form
that only this codebase understands. SAG already defines one — a grammar, a
parser and a minifier — so the canonical form is the minified `DO` statement and
equivalence is string equality over it.

Two things come free with that choice, and neither is cosmetic:

* The precondition stops being an opaque fingerprint and becomes a `BECAUSE`
  expression the GuardrailValidator can re-evaluate against fresh state. A stale
  action fails its own guardrail rather than needing a separate staleness check
  bolted alongside.
* The auto-approval rule becomes a `P:` policy clause — an expression evaluated
  by the same deterministic evaluator, stored as text, diffable and auditable.
  "Approved 12/12" is then arithmetic feeding an expression, with no model
  anywhere in the path.
"""

from __future__ import annotations

from typing import Any

from sag.context import MapContext
from sag.expression import ExpressionEvaluator
from sag.guardrail import GuardrailValidator
from sag.minifier import MessageMinifier
from sag.parser import SAGMessageParser

# Identity must not depend on when or by whom a statement was built, so the
# envelope is fixed. Only the DO line is ever compared.
_HEADER = "H v 1 id=a src=foreman dst=operator ts=0"


def literal(value: Any) -> str:
    """Render a Python value as a SAG literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def action_text(
    verb: str,
    args: dict[str, Any],
    *,
    reason: str | None = None,
    policy: str | None = None,
    policy_expr: str | None = None,
) -> str:
    """Build a `DO` statement. Args are emitted in sorted order so two callers
    that pass the same arguments produce the same text."""
    rendered = ",".join(f"{k}={literal(args[k])}" for k in sorted(args))
    out = f"DO {verb}({rendered})"
    if policy:
        out += f" P:{policy}" + (f":{policy_expr}" if policy_expr else "")
    if reason:
        out += f" BECAUSE {reason}"
    return out


def canonical(statement: str) -> str:
    """Round-trip a statement through parse and minify.

    The minifier's output is the canonical form, so this normalises away any
    formatting choice the caller made. Comparing canonical strings is the
    equivalence relation.
    """
    message = SAGMessageParser.parse(f"{_HEADER}\n{statement}")
    minified = MessageMinifier.to_minified_string(message)
    return minified.split("\n", 1)[1].rstrip(";")


def parse_action(statement: str):
    """Parse one `DO` statement into a SAG ActionStatement."""
    return SAGMessageParser.parse(f"{_HEADER}\n{statement}").statements[0]


def precondition_holds(statement: str, state: dict[str, Any]) -> tuple[bool, str | None]:
    """Re-evaluate an action's BECAUSE clause against current state.

    This is what makes a recorded action safe to replay: the guardrail is
    checked again at apply time, so an action computed against a file that has
    since changed fails rather than overwriting the change.
    """
    result = GuardrailValidator.validate(parse_action(statement), MapContext(state))
    return result.is_valid, result.error_message


def policy_allows(statement: str, ledger: dict[str, Any]) -> bool:
    """Evaluate an action's policy clause against approval statistics."""
    action = parse_action(statement)
    if not action.policy_expr:
        return False
    return bool(ExpressionEvaluator.evaluate(action.policy_expr, MapContext(ledger)))
