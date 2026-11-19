"""Verbatim copies of earlier user messages that state limits, permissions or changes.

Opt-in (``agents.defaults.preserveUserScopeMessages``). Replay history is cut by token
budget, message count and consolidation, so an early "do not change production.json"
can leave the prompt. This module keeps the user's own words, never a derived rule:

* Source: only the inbound channel message of a user turn. Tool results, documents,
  subagent output, automation triggers, commands, user messages injected mid-turn and
  runtime feedback are never recorded, because they never pass through ``record``.
* Selection: a recall-oriented pattern decides only whether a message is worth keeping
  verbatim. It does not decide what the message means, whether it is still valid, or
  whether it grants anything. A message whose only scope wording is a manner modifier of
  its own request ("X하지 말고 Y해줘", "without X") is not kept: it limited that request,
  which was handled in its own turn. The next few plain messages after an approval-like
  message are kept as follow-ups, because one of them may revoke it in other words.
* Replay: only quotes that are no longer in the replayed history are shown, newest first
  within a fixed budget. A message longer than MAX_ENTRY_CHARS is stored as its head and
  tail (a revocation often comes last) and is never treated as fully visible. Order comes
  from the persisted user rows (append-only, compaction deletes from the front), never from
  timestamps, whose formats vary. An earlier quote is shown in full only when every
  later user message is fully visible in the final prompt (replayed, or quoted untruncated
  in this block) and its order is known; otherwise only its limit parts are quoted, verbatim
  and in order, or nothing. Limits stay quoted (the failure direction is a needless
  question, not an unapproved action). This check does not depend on detecting grant words.
  Replay dependencies accompany the hidden quote as hashes; finalization removes any quote
  whose later replay rows were snipped, retaining only its limit excerpts.
* Scope: stored in the session's own metadata with the session key and workspace; quotes
  from another session key or workspace are never shown. ``Session.clear`` (``/new``)
  drops the ledger with the conversation.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from loguru import logger

from nanobot.session.history_visibility import is_hidden_history_message

LEDGER_KEY = "user_scope_messages"
LEDGER_VERSION = 1
MAX_STORED = 40          # oldest are dropped first and counted in "dropped"
MAX_ENTRY_CHARS = 500    # longer messages keep their head and are marked truncated
MAX_SHOWN = 10
MAX_BLOCK_CHARS = 3000
MAX_FOLLOW_UPS = 3       # plain messages kept after an approval-like one, so a revocation the
                         # pattern misses is still quoted with it; beyond that it is withheld

BLOCK_TAG = "[Earlier user messages — verbatim quotes outside the replayed history]"
BLOCK_END = "[/Earlier user messages]"
QUOTE_MESSAGE_META = "_user_scope_quotes"   # never persisted (AgentLoop._save_turn)

# Limits, permissions, revocations, changes and standing scope, in Korean and English.
# Deliberately broad: a false positive costs budget, a miss is an omission (documented).
_SCOPE_LANGUAGE = re.compile(
    r"(\S지\s*(?:마|말)|말고|금지|건드리지|손대지|제외|빼고|절대|지\s*않도록|면\s*안\s*(?:돼|된다|됨|되)|"
    r"그대로\s*(?:둬|두|유지)|놔\s*둬|유지해|삼가|"
    r"(?:만|에만|까지만|에서만)\s*(?:수정|변경|사용|읽|써|작업|허용|바꿔|고쳐|건드|손대|실행|돌려)|"
    r"\S도\s*(?:돼|되|된|됨|됩|좋|괜찮)|허용|승인|허락|괜찮아|"
    r"철회|취소|해제|풀어|없던\s*(?:일|걸로|것으로)|없었던\s*(?:일|걸로|것으로)|그만|보류|멈춰|중단|"
    r"원래대로|되돌려|잊어|무효|"
    r"대신|바꿔서|이제부터|지금부터|이제는|앞으로|더\s*이상|항상|매번|계속해서|계속\s*(?:유지|지켜)|당분간|"
    r"(?:번|회)까지만|로만\s*(?:답|설명)|열지도\s*마|"
    r"\buntil\b|\bat most\b|\bmust\s+not\b|\bmustn'?t\b|\byou\s+(?:may|can)\b|"
    r"\bdo\s+not\b|\bdon'?t\b|\bnever\b|\bonly\b|\bexcept\b|\bwithout\b|\ballow(?:ed)?\b|"
    r"\bapprove[ds]?\b|\bpermission\b|\brevoke[ds]?\b|\bcancel(?:led)?\b|\binstead\b|"
    r"\bfrom now on\b|\bno longer\b|\balways\b|\bscratch that\b|\bhold off\b|\bas is\b|"
    r"\bforget (?:it|that|about)\b|\bgo ahead\b|\bfeel free\b|\bok to\b|\bfine to\b|\bany ?more\b)",
    re.IGNORECASE,
)
# A match that is only a manner modifier of the same sentence's request.
_CONNECTIVE_TAIL = re.compile(r"\s*고")
_CONNECTIVE_WORDS = {"말고", "without", "instead", "대신"}
_FILE_TOKEN = re.compile(r"[\w.-]+\.[A-Za-z0-9]{1,6}\b|/")
_CLAUSE_START = re.compile(r"[.!?](?=\s)|[,;\n]")
_SENTENCE_START = re.compile(r"[.!?](?=\s)|\n")
# What keeps a "X하지 말고 Y" / "without X" modifier a standing limit rather than a one-off
# instruction about how to do that request: a period or frequency, or an operational target
# with lasting effect (server, production, deploy, repository action, network, install,
# delete). Answer-shaping modifiers ("도구를 사용하지 말고 한 문장으로", "검색하지 말고")
# stay one-off unless a period is given. Not proof of meaning; misses are documented.
_STANDING = re.compile(
    r"동안|이번\s*(?:작업|대화|세션|프로젝트)|앞으로|이제부터|계속|항상|매번|당분간|오늘은|"
    r"(?:한|두|세|\d+)\s*(?:번|회)\s*(?:만|까지|이상|이하|넘게)|최대\s*\d+|"
    r"서버|운영|프로덕션|배포|커밋|푸시|머지|네트워크|원격|데이터베이스|\bDB\b|설치|삭제|"
    r"재시작|sudo|권한|마이그레이션|기다려|\bmigration\b|\bwait\b|"
    r"\bduring\b|\bfor (?:this|the rest of)\b|\bfrom now on\b|\buntil\b|\bany(?:more| more)\b|"
    r"\bonce\b|\btwice\b|\bat most\b|\b\d+ times\b|\bserver|"
    r"\bprod(?:uction)?\b|\bdeploy|\bcommit|\bpush|\bmerge|\bnetwork|\bremote\b|\bdatabase|"
    r"\binstall|\bdelete|\brestart|\bsudo\b",
    re.IGNORECASE,
)
# Wording that could grant something. Broad on purpose: it only decides when to withhold.
_APPROVAL_LIKE = re.compile(
    r"\S도\s*(?:돼|되|된|됨|됩|좋|괜찮)|허용|승인|허락|괜찮|진행|그렇게\s*해|좋아|오케이|ㅇㅋ|"
    r"해제|풀어|\bok(?:ay)?\b|\bgo ahead\b|\ballow|\bapprov|\bpermi|\bfeel free\b|"
    r"\byou (?:can|may)\b|\bfine to\b",
    re.IGNORECASE,
)
# Negative limits and revocations ("do not ...", "승인 철회"). Used to keep the limit part of a
# message whose other part is withheld, and so that "승인 철회" does not count as a grant. A miss
# loses that limit (as without this module); the grant check after it still applies.
_LIMIT = re.compile(
    r"\S지\s*(?:마|말)|금지|건드리지|손대지|절대|지\s*않도록|면\s*안\s*(?:돼|된다|됨|되)|"
    r"그대로\s*(?:둬|두)|놔\s*둬|삼가|철회|취소|무효|보류|멈춰|중단|그만|되돌려|원래대로|"
    r"없던\s*(?:일|걸로|것으로)|없었던\s*(?:일|걸로|것으로)|"
    r"\bdo\s+not\b|\bdon'?t\b|\bnever\b|\bmust\s+not\b|\bmustn'?t\b|\bno longer\b|"
    r"\brevoke|\bcancel|\bhold off\b|\bscratch that\b|\bnever ?mind\b|\bwithdraw",
    re.IGNORECASE,
)
_SEGMENT_BOUNDARY = re.compile(
    r"[.!?](?=\s|$)|[,;\n]|(?:지만|는데|으며|면서)\s|(?<=[되돼])고\s|\s(?:but|and|while)\s",
    re.IGNORECASE,
)
_HEAD_CHARS = 300        # a truncated message keeps its head and its tail (where a closing
_TAIL_CHARS = MAX_ENTRY_CHARS - _HEAD_CHARS   # "...so I revoke that" usually sits)
# A small closed grammar for reply formatting, containing no arbitrary action/object slot.
# These preferences can be retained as limit excerpts without reviving an old action request.
_REPLY_STYLE = re.compile(
    r"(?:앞으로|이제부터|계속해서|항상)\s*(?:(?:답변|응답)은?\s*)?(?:항상\s*)?"
    r"(?:자세히|간략히|짧게|길게|한국어로만|영어로만)\s*(?:설명해|답해|써)(?:\s*줘)?[.!]?",
)


def has_scope_language(text: str) -> bool:
    return bool(_SCOPE_LANGUAGE.search(text or ""))


def _is_connective(text: str, match: re.Match[str]) -> bool:
    word = match.group(0).strip().lower()
    if word in _CONNECTIVE_WORDS:
        pass
    elif re.fullmatch(r"\S지\s*말", word) and _CONNECTIVE_TAIL.match(text, match.end()):
        pass
    else:
        return False
    starts = [m.end() for m in _CLAUSE_START.finditer(text, 0, match.start())]
    clause = text[starts[-1] if starts else 0:match.end()]
    if _FILE_TOKEN.search(clause):
        return False                    # a named file or path keeps it a standing limit
    sentence_starts = [m.end() for m in _SENTENCE_START.finditer(text, 0, match.start())]
    ends = _SENTENCE_START.search(text, match.end())
    sentence = text[sentence_starts[-1] if sentence_starts else 0:ends.start() if ends else len(text)]
    return not _STANDING.search(sentence)   # a period, frequency or resource keeps it standing


def should_record(text: str) -> bool:
    """Keep only messages with scope wording beyond a modifier of their own request."""
    text = text or ""
    # Quoted source blocks and questions about wording are not standing instructions.
    # The original message, not this selection-only view, is stored if it is selected.
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"^---\s*$.*?^---\s*$", "", text, flags=re.S | re.M).strip()
    if re.fullmatch(r'(?:what (?:does|is)|is it|are (?:we|you)|can i)\b[^?\n]*\?'
                    r'(?:\s*["“][^\n]*["”])?', text, re.I):
        return False
    if (re.search(r"(?:무슨\s*뜻(?:이야|인가요)?|뜻이\s*뭐(?:야|예요)?|였던가|괜찮아\s*보여)\??\s*$", text)
            and not _CLAUSE_START.search(text.rstrip("?!"))):
        return False
    if re.fullmatch(r"(?:천천히|편하게)\s*해도\s*(?:돼|괜찮아)[.!]?", text):
        return False
    return any(not _is_connective(text, m) for m in _SCOPE_LANGUAGE.finditer(text))


def limit_segments(text: str) -> list[tuple[int, int]]:
    """Spans that state a negative limit or a revocation, cut before any later grant wording."""
    text = text or ""
    bounds = [m.end() for m in _SEGMENT_BOUNDARY.finditer(text)]
    spans: list[tuple[int, int]] = []
    for match in _LIMIT.finditer(text):
        start = max([b for b in bounds if b <= match.start()], default=0)
        end = min([b for b in bounds if b > match.end()], default=len(text))
        if _APPROVAL_LIKE.search(text, match.end(), end):
            end = match.end()           # "금지는 풀어도 돼": keep "…금지", not the grant after it
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))
    return [(a, b) for a, b in spans if text[a:b].strip()]


def approval_like(text: str) -> bool:
    """Grant-like wording outside the message's own limits ("진행하지 마", "승인 철회" grant nothing)."""
    text = text or ""
    rest, last = [], 0
    for start, end in limit_segments(text):
        rest.append(text[last:start])
        last = end
    rest.append(text[last:])
    return bool(_APPROVAL_LIKE.search(" ".join(rest)))


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content
                         if isinstance(b, Mapping) and b.get("type") == "text")
    return ""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _ledger(metadata: Mapping[str, Any] | None, session_key: str) -> dict[str, Any] | None:
    """Return a well-formed ledger for this session, or None (missing, foreign, malformed)."""
    raw = metadata.get(LEDGER_KEY) if isinstance(metadata, Mapping) else None
    if (not isinstance(raw, dict) or raw.get("v") != LEDGER_VERSION
            or raw.get("session_key") != session_key or not isinstance(raw.get("entries"), list)):
        return None
    if type(raw.get("dropped", 0)) is not int or raw.get("dropped", 0) < 0:
        logger.warning("Ignoring malformed user scope ledger counter")
        return None
    for entry in raw["entries"]:
        if not isinstance(entry, dict):
            continue
        if (type(entry.get("truncated", False)) is not bool
                or ("chars" in entry and (type(entry["chars"]) is not int or entry["chars"] < 0))
                or ("tail" in entry and not isinstance(entry["tail"], str))):
            logger.warning("Ignoring malformed user scope ledger entry")
            return None
    return raw


def record(
    metadata: dict[str, Any],
    *,
    session_key: str,
    workspace: str,
    text: str,
    timestamp: str,
    message_id: str | None = None,
    channel: str | None = None,
) -> bool:
    """Keep a verbatim copy of a genuine user message when it states scope. Returns True if kept."""
    text = (text or "").strip()
    if not text:
        return False
    ledger = _ledger(metadata, session_key)
    follow_up = False
    if not should_record(text):
        # Right after an approval-like message, keep the next few plain messages too: one of
        # them may revoke it in words the pattern does not know.
        follow_up = ledger is not None and _follow_up_open(ledger["entries"])
        if not follow_up:
            return False
    if ledger is None:
        # Missing, malformed, or copied from another session: start this session's own ledger.
        ledger = {"v": LEDGER_VERSION, "session_key": session_key, "entries": [], "dropped": 0}
    timestamp = str(timestamp or "")
    entry_id = hashlib.sha256(f"{message_id or ''}\0{timestamp}\0{text}".encode()).hexdigest()[:16]
    if any(isinstance(e, dict) and e.get("id") == entry_id for e in ledger["entries"]):
        return False
    truncated = len(text) > MAX_ENTRY_CHARS
    ledger["entries"].append({
        "id": entry_id,
        "ts": timestamp,
        "message_id": message_id,
        "channel": channel,
        "workspace": workspace,
        "text": text[:_HEAD_CHARS] if truncated else text,
        **({"tail": text[-_TAIL_CHARS:], "chars": len(text)} if truncated else {}),
        "full_sha256": _sha(text),
        "truncated": truncated,
        **({"follow_up": True} if follow_up else {}),
    })
    overflow = len(ledger["entries"]) - MAX_STORED
    if overflow > 0:
        del ledger["entries"][:overflow]
        ledger["dropped"] = int(ledger.get("dropped") or 0) + overflow
    metadata[LEDGER_KEY] = ledger
    return True


def _follow_up_open(entries: Sequence[Any]) -> bool:
    follow_ups = 0
    for entry in reversed(entries):
        if not isinstance(entry, dict):
            continue
        if entry.get("follow_up"):
            follow_ups += 1
        elif approval_like(str(entry.get("text") or "")):
            return follow_ups < MAX_FOLLOW_UPS
    return False


def _in_replay(entry: Mapping[str, Any], replayed_user_texts: Sequence[str]) -> bool:
    """Same message still replayed in full: equal text, or text plus replay breadcrumbs."""
    text = str(entry.get("text") or "")
    for replayed in replayed_user_texts:
        if not entry.get("truncated"):
            if replayed == text or (replayed.startswith(text) and replayed[len(text):][:1] == "\n"):
                return True
        elif _sha(replayed) == entry.get("full_sha256"):
            return True
        elif type(entry.get("chars")) is int:
            length = entry["chars"]
            if (len(replayed) > length and replayed[length:][:1] == "\n"
                    and _sha(replayed[:length]) == entry.get("full_sha256")):
                return True  # a complete original plus runtime breadcrumbs, never head+tail alone
    return False


def _user_rows(session_messages: Sequence[Mapping[str, Any]]) -> list[str]:
    rows = []
    for row in session_messages:
        if isinstance(row, Mapping) and row.get("role") == "user" and not is_hidden_history_message(row):
            rows.append(_text_of(row.get("content")).strip())
    return rows


def _place(entries: Sequence[Mapping[str, Any]], rows: Sequence[str]) -> tuple[list[int | None], bool]:
    """Row index of each entry, in persisted order (never by timestamp).

    Rows are only appended, or deleted from the front (compaction, file cap), so entries whose
    row is gone must come before every placed one. Anything else means the order is unknown.
    """
    shas = [_sha(r) for r in rows]
    places: list[int | None] = []
    cursor, placed_any, consistent = 0, False, True
    for entry in entries:
        want = str(entry.get("full_sha256") or "")
        index = next((i for i in range(cursor, len(shas)) if shas[i] == want), None)
        if index is None:
            consistent = consistent and not placed_any
            places.append(None)
        else:
            placed_any = True
            places.append(index)
            cursor = index + 1
    return places, consistent


def _display(entry: Mapping[str, Any]) -> str:
    if not entry.get("truncated"):
        return entry["text"]
    tail = str(entry.get("tail") or "")
    hidden = int(entry.get("chars") or 0) - len(entry["text"]) - len(tail)
    gap = f" [… {hidden} characters not shown …] " if hidden > 0 else " [… rest not shown …] "
    return entry["text"] + gap + tail


def _excerpt_parts(entry: Mapping[str, Any]) -> list[str]:
    """Limit and revocation parts of the stored head and tail, in their original order."""
    parts = []
    for text in (str(entry.get("text") or ""), str(entry.get("tail") or "")):
        if _REPLY_STYLE.fullmatch(text.strip()):
            parts.append(text.strip())
            continue
        parts += [text[a:b].strip() for a, b in limit_segments(text)]
    return parts


def _excerpt(entry: Mapping[str, Any]) -> str:
    return " … ".join(f"“{part}”" for part in _excerpt_parts(entry))


def build_block(
    metadata: Mapping[str, Any] | None,
    *,
    session_key: str,
    workspace: str,
    history: Sequence[Mapping[str, Any]],
    current_text: str,
    session_messages: Sequence[Mapping[str, Any]] | None = None,
    visibility: dict[str, Any] | None = None,
) -> str | None:
    """Quote earlier scope messages that the replayed history no longer contains.

    Invariant, independent of grant-word detection: an earlier quote is
    shown in full only if it is complete and every later user message of this session is fully
    visible (replayed in full, or quoted in full here). Otherwise only its negative-limit parts
    are quoted, or nothing. ``session_messages`` are the persisted rows in their stored order;
    without them the order is unknown and no grant-like quote is shown in full.
    """
    ledger = _ledger(metadata, session_key)
    if visibility is not None:
        visibility.clear()
    if ledger is None:
        return None
    replayed = [_text_of(m.get("content")).strip() for m in history
                if m.get("role") == "user" and not is_hidden_history_message(m)]
    replayed.append((current_text or "").strip())
    entries = [e for e in ledger["entries"] if isinstance(e, dict) and isinstance(e.get("text"), str)]
    rows = _user_rows(session_messages) if session_messages is not None else None
    current = (current_text or "").strip()
    if rows is not None and current and (not rows or rows[-1] != current):
        rows.append(current)             # the current message may not be persisted yet
    places, consistent = _place(entries, rows) if rows is not None else ([None] * len(entries), False)
    place_of = {id(e): p for e, p in zip(entries, places)}
    row_entry = {p: e for e, p in zip(entries, places) if p is not None}
    replay_shas = {_sha(t) for t in replayed}

    eligible = [e for e in entries if e.get("workspace") == workspace]
    missing = [e for e in eligible if not _in_replay(e, replayed)]
    if not missing:
        return None
    shown: list[dict] = []
    used = 0
    for entry in reversed(missing):            # newest first, stop at the first that does not fit
        cost = len(_display(entry)) + 40
        if len(shown) >= MAX_SHOWN or used + cost > MAX_BLOCK_CHARS:
            break
        shown.append(entry)
        used += cost
    shown.reverse()
    omitted = len(missing) - len(shown) + int(ledger.get("dropped") or 0)

    excerpt: set[int] = set()                  # id() of entries reduced to their limit parts
    while True:
        full = [e for e in shown if id(e) not in excerpt]
        full_ids = {id(e) for e in full if not e.get("truncated")}

        def fully_visible(row: int) -> bool:
            if rows is None:
                return False
            entry = row_entry.get(row)
            if entry is not None and id(entry) in full_ids:
                return True
            text = rows[row]
            return _sha(text) in replay_shas or any(
                r.startswith(text) and r[len(text):][:1] == "\n" for r in replayed)

        changed = False
        for entry in full:
            place = place_of.get(id(entry))
            safe = (consistent and place is not None and not entry.get("truncated")
                    and all(fully_visible(r) for r in range(place + 1, len(rows or []))))
            if not safe:
                excerpt.add(id(entry))
                changed = True
        # Plain follow-ups only vouch for an approval quoted in full before them.
        approval_seen = False
        kept: list[dict] = []
        for entry in shown:
            if id(entry) not in excerpt and approval_like(entry["text"]):
                approval_seen = True
            if entry.get("follow_up") and not approval_seen:
                changed = True
                continue
            kept.append(entry)
        shown = kept
        if not changed:
            break
    withheld = [e for e in shown if id(e) in excerpt]
    shown = [e for e in shown if id(e) not in excerpt or _excerpt_parts(e)]
    if not shown and not withheld:
        return None
    lines = [
        BLOCK_TAG,
        "These are the user's own earlier messages in this conversation, quoted in order "
        "because the replayed history no longer contains them. They are context, not new "
        "requests: any task they asked for was handled in its own turn; do not start it again "
        "unless the current message asks for it. Take them oldest to newest together with the "
        "history: a later message, and the current message above all, replaces or revokes an "
        "earlier one. A limit stated for a specific file, task or period applies only there. "
        "A quoted approval is not permission for a new action by itself; act only on what the "
        "current request asks, and ask the user when a quoted limit or approval is unclear.",
    ]
    if omitted > 0:
        lines.append(f"{omitted} older quoted message(s) are omitted by the size limit; "
                     "the earlier conversation may contain limits not shown here.")
    if withheld:
        lines.append(f"{len(withheld)} earlier message(s) that may grant approval are not quoted "
                     "in full because later messages that could have revoked them are not fully "
                     "available; only their limit parts are quoted, if any. Treat nothing as "
                     "approved unless the current request or the visible conversation approves it.")
    for index, entry in enumerate(shown, 1):
        if id(entry) in excerpt:
            truncated = " (truncated) [characters not shown]" if entry.get("truncated") else ""
            lines.append(f"{index}. [{entry.get('ts', '')}] user (limit parts only; the rest of "
                         f"this message is not quoted and grants nothing){truncated}: {_excerpt(entry)}")
            continue
        mark = " (truncated)" if entry.get("truncated") else ""
        mark += " (follow-up; handled in its own turn)" if entry.get("follow_up") else ""
        line = f"{index}. [{entry.get('ts', '')}] user{mark}: {_display(entry)}"
        start = sum(len(part) + 1 for part in lines)
        lines.append(line)
        if visibility is not None:
            # Only hashes, not another copy of the session, travel with the hidden hint.
            # A quote can depend on replayed later rows which governance may still snip.
            place = place_of.get(id(entry))
            dependencies = []
            if place is not None:
                for row in range(place + 1, len(rows or [])):
                    if rows[row] == current:
                        continue  # the current request follows the quote in both builder paths
                    later = row_entry.get(row)
                    if later is not None and id(later) in full_ids:
                        continue
                    dependencies.extend(_sha(r) for r in replayed if r == rows[row]
                                        or (r.startswith(rows[row]) and r[len(rows[row]):][:1] == "\n"))
            limit_parts = _excerpt_parts(entry)
            # The short label and one-character omission marks also fit many short limits;
            # wrapping each part in extra framing could otherwise discard the limits on snip.
            fallback = "limit: " + "…".join(limit_parts) if limit_parts else ""
            visibility[line] = {"start": start, "requires": sorted(set(dependencies)),
                                "fallback": fallback}
    lines.append(BLOCK_END)
    return "\n".join(lines)


def quote_message(block: str, visibility: dict[str, Any] | None = None) -> dict[str, Any]:
    """The block as its own hidden message: not the current request, never persisted."""
    return {"role": "user", "_hidden_history": True, QUOTE_MESSAGE_META: True, "content": block,
            **({"_scope_quote_visibility": visibility} if visibility else {})}


def finalize_quotes(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Downgrade hints whose replay dependencies were removed, on the final model copy only.

    This runs after *all* context repair paths on every model call. It never expands a hint,
    mutates the persisted conversation, or relies on recognizing approval wording.
    """
    if not any(m.get(QUOTE_MESSAGE_META) and m.get("_scope_quote_visibility") for m in messages):
        return messages
    replayed = {_sha(_text_of(m.get("content")).strip()) for m in messages
                if m.get("role") == "user" and not is_hidden_history_message(m)}
    updated = messages
    for index, message in enumerate(messages):
        dependencies = message.get("_scope_quote_visibility")
        if not message.get(QUOTE_MESSAGE_META) or not isinstance(dependencies, dict):
            continue
        original = message["content"]
        parts, cursor = [], 0
        # Positions, rather than splitlines/string replacement, preserve multi-line messages
        # and cannot confuse quoted lookalike entries inside another user's original text.
        for line, entry in sorted(dependencies.items(), key=lambda item: item[1]["start"]):
            start = entry["start"]
            if start < cursor or original[start:start + len(line)] != line:
                parts = ["Earlier user quotes were omitted because their context could not be verified."]
                cursor = len(original)
                break
            parts.append(original[cursor:start])
            if set(entry["requires"]).issubset(replayed):
                parts.append(line)
            else:
                fallback = entry["fallback"]
                parts.append(fallback if len(fallback) <= len(line) else "")
            cursor = start + len(line)
        content = "".join(parts) + original[cursor:]
        if updated is messages:
            updated = list(messages)
        updated[index] = {k: v for k, v in message.items() if k != "_scope_quote_visibility"}
        updated[index]["content"] = content
    return updated
