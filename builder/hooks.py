"""Source generators for tool hooks and code nodes.

Every function takes a plain dict `cfg` that the compiler builds from the spec and the openapi
(no value is written here; texts below are generic instructions to the model) and returns
Python source for the platform sandbox. Rules every generated source follows:
- no import statements (the sandbox's imports are unverified for hooks): dates are compared as
  ISO strings against lists the facts node wrote into vars (`business_days`, `callback_dates`);
- no doubled brace anywhere (`literal` renders nested containers with a space before every
  closing brace and escapes doubled braces inside strings);
- all logic lives in one function `_hook`, called with vars/config/result as arguments, so it
  works whatever exec layout the platform uses (separate globals and locals included);
- everything is wrapped in try/except: pre-hooks leave the request as the model sent it (the
  close hook writes the compile-time fallback instead), post-hooks leave the result unchanged.

Variables hooks write (the compiler checks them against every model-filled field):
identity_state, identity_wrong, identity_verified_count, callback_state, callback_invalid_count,
callback_guard, callback_sent_date, callback_sent_time, promise_idx, promise_guard,
promise_asked_date, promise_asked_amount, promise_sent_date, promise_sent_amount,
promise_changed, promise_state, p<k>_promise, p<k>_promise_fails, p<k>_promise_date,
p<k>_promise_amount, p<k>_note, p<k>_note_fails, note_idx, note_guard, note_sent_reason,
note_blocked_hit, notes_blocked, transfer_sent_queue, transfer_state, transfer_reason,
closing_ready, final_outcome, final_reason.
"""

from __future__ import annotations

import ast
import textwrap
from typing import Any

from builder.snippets import ENGINE_PATH, _StripForSnippet

# ---------------------------------------------------------------------------------------------
# Texts the post-hooks hand to the model as `what_to_do_next`. Easy to change in one place; a
# cfg key `texts` (dict of the same keys) overrides any of them.
# ---------------------------------------------------------------------------------------------

DEFAULT_TEXTS: dict[str, str] = {
    "ask_again": (
        "The answer was rejected or could not be read. It was not counted as a wrong answer. "
        "Ask for it again in the required format."
    ),
    "wrong": "That answer was not accepted.",
    "next_question": "That answer was accepted. Ask the next security question.",
    "verified": "All security questions are passed. Ask nothing more; the call moves on by itself.",
    "failed": "Verification failed. Ask nothing more; the call moves on by itself.",
    "cb_reask": (
        "That date or time is outside the callback window and was not recorded. "
        "Ask for another time inside the window, then call the tool again."
    ),
    "cb_none": "No valid callback time was found and nothing was recorded. Follow your instructions for this case and call ",
    "cb_done": "The callback is recorded for ",
    "cb_done_exit": ". Tell the contact, say the goodbye text from your instructions, then call ",
    "cb_fail": "The callback was not recorded. Do not say it was.",
    "recorded": "The promise is recorded: ",
    "differs": "These values differ from what the customer asked for. Tell the customer the recorded values.",
    "success": " Say this sentence to the customer: ",
    "next_product": " Next product to handle: ",
    "next_product_tools": " Follow your instructions for it: record a promise or add a note.",
    "retry": "The promise was not recorded. Call the tool again with exactly the same values. Do not say it is recorded.",
    "note_for_failed": "The promise could not be recorded. Do not say it is recorded. Call the note tool for this product with reason ",
    "write_failed": "The promise could not be recorded. Do not say it is recorded; the call moves on by itself.",
    "note_ok": "The note is recorded.",
    "note_retry": "The note was not recorded. Call the tool once more with the same values.",
    "note_gave_up": "The note could not be recorded. Move on.",
    "transfer_ok": "The transfer is done. Tell the customer they are being connected.",
    "transfer_fail": "The transfer failed. Do not say the customer is being connected.",
    "finish": "Everything is handled.",
}

# Closing words per hook, filled by the compiler from the spec; absent -> nothing is added.
SLOT_FIELDS: tuple[str, ...] = ("id", "name", "full", "floor", "deadline")


# ---------------------------------------------------------------------------------------------
# Literal rendering and checks
# ---------------------------------------------------------------------------------------------


def _str_literal(text: str) -> str:
    out = repr(text)
    out = out.replace("{{", "{\\x7b").replace("}}", "}\\x7d")
    return out


def literal(obj: Any) -> str:
    """Python source for dict/list/tuple/str/int/float/bool/None with no doubled brace.

    Dicts end with ' }' (space before the closing brace), so a dict nested last in another
    never yields '}}'; braces inside strings are escaped when they would double.
    """
    if obj is None:
        return "None"
    if isinstance(obj, bool):
        return "True" if obj else "False"
    if isinstance(obj, (int, float)):
        return repr(obj)
    if isinstance(obj, str):
        return _str_literal(obj)
    if isinstance(obj, (list, tuple)):
        return "[" + ", ".join(literal(x) for x in obj) + "]"
    if isinstance(obj, dict):
        if not obj:
            return "{ }"
        return "{" + ", ".join(literal(k) + ": " + literal(v) for k, v in obj.items()) + " }"
    raise TypeError("cannot render " + type(obj).__name__)


def check_source(source: str) -> str:
    """Return source unchanged if it parses, has no import and no doubled brace; else raise."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError("generated source does not parse: " + str(exc)) from exc
    if "{{" in source or "}}" in source:
        raise ValueError("generated source contains a doubled brace")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError("generated source contains an import")
    return source


def engine_function_source(name: str) -> str:
    """Source of one top-level engine function, docstring and annotations stripped."""
    tree = ast.parse(ENGINE_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            module = ast.Module(body=[node], type_ignores=[])
            module = _StripForSnippet().visit(module)
            ast.fix_missing_locations(module)
            return ast.unparse(module)
    raise KeyError("engine has no function " + name)


def _texts(cfg: dict) -> dict[str, str]:
    out = dict(DEFAULT_TEXTS)
    extra = cfg.get("texts")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if isinstance(v, str):
                out[k] = v
    return out


def _assemble(
    kind: str,
    consts: list[tuple[str, Any]],
    helpers: list[str],
    body: str,
    fallback: str = "pass",
    top: list[tuple[str, Any]] | None = None,
) -> str:
    """Wrap constants, helper functions and a body into one `_hook` function inside try/except."""
    params = {"pre": "vars, config", "post": "vars, result", "code": "vars"}[kind]
    inner: list[str] = []
    for name, value in consts:
        inner.append(name + " = " + literal(value))
    for h in helpers:
        inner.append(textwrap.dedent(h).strip("\n"))
    inner.append(textwrap.dedent(body).strip("\n"))
    func = "def _hook(" + params + "):\n" + textwrap.indent("\n".join(inner), "    ")
    lines: list[str] = []
    for name, value in top or []:
        lines.append(name + " = " + literal(value))
    lines.append("try:")
    lines.append(textwrap.indent(func, "    "))
    lines.append("    _hook(" + params + ")")
    lines.append("except Exception:")
    lines.append(textwrap.indent(textwrap.dedent(fallback).strip("\n"), "    "))
    return check_source("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------------------------
# Shared helper functions (static source, embedded inside `_hook`)
# ---------------------------------------------------------------------------------------------

_H_BASE = """
def _s(x):
    if x is None:
        return ''
    return str(x)
def _num(x):
    try:
        v = float(x)
    except Exception:
        return None
    if v != v or v in (float('inf'), float('-inf')):
        return None
    return v
def _int(x):
    v = _num(x)
    if v is None:
        return 0
    return int(v)
def _true(x):
    return x is True or str(x).lower() == 'true'
def _key(k, name):
    return 'p' + str(k) + '_' + name
def _get(k, name):
    return vars.get(_key(k, name))
def _money(x):
    v = _num(x)
    if v is None:
        return ''
    return format(v, '.' + str(DEC) + 'f')
"""

# The same helpers without the money function (hooks that have no DEC constant).
_H_BASE_NODEC = _H_BASE.split("def _money")[0]

_H_DATE = """
def _valid_date(d):
    if len(d) != 10 or d[4] != '-' or d[7] != '-':
        return False
    if not (d[:4].isdigit() and d[5:7].isdigit() and d[8:].isdigit()):
        return False
    y = int(d[:4])
    m = int(d[5:7])
    day = int(d[8:])
    if m < 1 or m > 12 or day < 1:
        return False
    dim = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    if m == 2 and y % 4 == 0 and (y % 100 != 0 or y % 400 == 0):
        dim = 29
    return day <= dim
"""

_H_ACTIVE = """
def _active():
    ks = []
    for k in range(1, N + 1):
        if _s(_get(k, 'id')) != '':
            ks.append(k)
    return ks
def _pending(k):
    return _s(_get(k, 'promise')) != 'recorded' and _s(_get(k, 'note')) == ''
def _all_handled():
    for k in _active():
        if _pending(k):
            return False
    return True
def _next_pending(skip):
    for k in _active():
        if k != skip and _pending(k) and _s(_get(k, 'promise')) != 'failed':
            return k
    return 0
"""

_H_PICK = """
def _pick(model_id):
    ks = _active()
    if not MULTI:
        if 1 in ks:
            return 1
        return 0
    for k in ks:
        if _s(_get(k, 'id')) == _s(model_id):
            return k
    for k in ks:
        if _pending(k):
            return k
    return 0
"""

_H_NEXT_TEXT = """
def _next_text(k):
    if _all_handled():
        vars['closing_ready'] = 'yes'
        return ' ' + FINISH
    nk = _next_pending(k)
    if nk == 0:
        return ''
    return T_NEXT_PRODUCT + _s(_get(nk, 'name')) + ' (id ' + _s(_get(nk, 'id')) + ').' + T_NEXT_TOOLS
"""


def _finish_text(cfg: dict, t: dict[str, str]) -> str:
    """'Everything is handled. <goodbye> Then call <finalize transition>.'"""
    parts = [t["finish"]]
    goodbye = cfg.get("goodbye")
    if goodbye:
        parts.append("Say the goodbye text: " + goodbye)
    else:
        parts.append("Say goodbye if your instructions say so.")
    transition = cfg.get("finalize_transition")
    if transition:
        parts.append("Then call " + transition + ".")
    return " ".join(parts)


def _ok_key(cfg: dict) -> str:
    return str(cfg.get("ok_key") or "ok")


# ---------------------------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------------------------

_VERIFY_BODY = """
if isinstance(result, dict):
    result.pop('header', None)
    data = result.get('data')
else:
    data = None
if isinstance(data, dict):
    state = _s(vars.get('identity_state')) or 'pending'
    wrong = _int(vars.get('identity_wrong'))
    vcount = _int(vars.get('identity_verified_count'))
    text = ''
    ok = True
    if RESP_OK != '':
        ok = _true(data.get(RESP_OK))
    v = None
    av = None
    hint = None
    wl = None
    if ok:
        if RESP_VERIFIED != '':
            v = data.get(RESP_VERIFIED)
        if RESP_ALL != '':
            av = data.get(RESP_ALL)
        if RESP_HINT != '':
            hint = data.get(RESP_HINT)
        if RESP_LEFT != '':
            wl = _num(data.get(RESP_LEFT))
        if _true(v):
            vcount += 1
        if (RESP_VERIFIED != '' or RESP_ALL != '') and not _true(v) and not _true(av):
            matched = None
            for h in HINTS:
                if hint is not None and _s(hint) == _s(h.get('value')):
                    matched = h
                    break
            if matched is not None and not matched.get('counts_as_wrong'):
                text = _s(matched.get('instruction'))
            else:
                wrong += 1
                text = T_WRONG
                if matched is not None:
                    text = text + ' ' + _s(matched.get('instruction'))
        if state != 'verified':
            if _true(av) or (av is None and NQ > 0 and vcount >= NQ):
                state = 'verified'
            elif not _true(v) and ((MAXW is not None and wrong >= MAXW) or (wl is not None and wl <= 0)):
                state = 'failed'
    else:
        text = T_ASK_AGAIN
    if state == 'verified':
        text = T_VERIFIED
    elif state == 'failed':
        text = T_FAILED
    elif text == '':
        text = T_NEXT_QUESTION
    vars['identity_state'] = state
    vars['identity_wrong'] = wrong
    vars['identity_verified_count'] = vcount
    data['what_to_do_next'] = text
"""


def verify_post_hook(cfg: dict) -> str:
    """Post-hook of the identity tool.

    cfg keys:
      resp: dict with keys ok, verified, all_verified, hint, wrong_left -> response key name
            ('' or missing switches that feature off);
      max_wrong: int | None (identity.max_wrong_total);
      questions: int, number of identity questions;
      hints: list of dicts value / instruction / counts_as_wrong (identity.hints);
      texts: optional overrides of DEFAULT_TEXTS.
    Writes vars identity_state (pending|verified|failed), identity_wrong,
    identity_verified_count and result['data']['what_to_do_next']; drops result['header'].
    """
    t = _texts(cfg)
    resp = cfg.get("resp") or {}
    consts = [
        ("RESP_OK", str(resp.get("ok") or "")),
        ("RESP_VERIFIED", str(resp.get("verified") or "")),
        ("RESP_ALL", str(resp.get("all_verified") or "")),
        ("RESP_HINT", str(resp.get("hint") or "")),
        ("RESP_LEFT", str(resp.get("wrong_left") or "")),
        ("MAXW", cfg.get("max_wrong")),
        ("NQ", int(cfg.get("questions") or 0)),
        ("HINTS", list(cfg.get("hints") or [])),
        ("T_WRONG", t["wrong"]),
        ("T_ASK_AGAIN", t["ask_again"]),
        ("T_VERIFIED", t["verified"]),
        ("T_FAILED", t["failed"]),
        ("T_NEXT_QUESTION", t["next_question"]),
    ]
    return _assemble("post", consts, [_H_BASE_NODEC], _VERIFY_BODY)


# ---------------------------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------------------------

_CB_PRE_BODY = """
body = config.get('body')
if isinstance(body, dict):
    raw_date = _s(body.get(F_DATE)).strip()
    raw_time = _s(body.get(F_TIME)).strip()
    vars['callback_sent_date'] = raw_date
    vars['callback_sent_time'] = raw_time
    if GAPS:
        vars['callback_guard'] = 'skipped'
        vars['callback_state'] = ''
    else:
        d = raw_date[:10]
        parts = raw_time.split(':')
        mins = None
        secs = 0
        if len(parts) in (2, 3) and len(parts[0]) == 2 and len(parts[1]) == 2 and parts[0].isdigit() and parts[1].isdigit():
            hh = int(parts[0])
            mm = int(parts[1])
            if len(parts) == 3 and parts[2][:2].isdigit():
                secs = int(parts[2][:2])
            if hh < 24 and mm < 60:
                mins = hh * 60 + mm
        dates = []
        for x in _s(vars.get('callback_dates')).split(','):
            if x != '':
                dates.append(x)
        good = mins is not None and d in dates
        if good and HS is not None and mins < HS:
            good = False
        if good and HE is not None:
            if INCL:
                late = mins > HE or (mins == HE and secs > 0)
            else:
                late = mins >= HE
            if late:
                good = False
        if good and AFTER_NOW and d == _s(vars.get('today')):
            np = _s(vars.get('now_time')).split(':')
            if len(np) == 2 and np[0].isdigit() and np[1].isdigit():
                if mins * 60 + secs <= (int(np[0]) * 60 + int(np[1])) * 60:
                    good = False
            else:
                good = False
        if good:
            vars['callback_state'] = ''
        else:
            body[F_DATE] = ''
            body[F_TIME] = ''
            vars['callback_state'] = 'invalid'
            vars['callback_invalid_count'] = _int(vars.get('callback_invalid_count')) + 1
"""


def _hhmm_minutes(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    return int(parts[0]) * 60 + int(parts[1])


def callback_pre_hook(cfg: dict) -> str:
    """Pre-hook of the callback tool: blank an invalid date/time so the API rejects it.

    cfg keys:
      fields: dict date / time -> body field names;
      hour_start, hour_end: 'HH:MM' strings (or None);
      inclusive, must_be_after_now: bool | None (None means the policy is silent);
      gaps: bool, True when the window is not fully defined (engine.callback_window_gaps) -
            the hook then leaves the request unchanged and sets vars['callback_guard']='skipped'.
    Reads vars callback_dates (comma-joined ISO dates), today, now_time. Writes callback_state
    ('' valid / skipped, 'invalid'), callback_invalid_count, callback_sent_date, callback_sent_time.
    """
    f = cfg.get("fields") or {}
    gaps = bool(cfg.get("gaps")) or cfg.get("inclusive") is None or cfg.get("must_be_after_now") is None
    consts = [
        ("F_DATE", str(f.get("date") or "date")),
        ("F_TIME", str(f.get("time") or "time")),
        ("HS", _hhmm_minutes(cfg.get("hour_start"))),
        ("HE", _hhmm_minutes(cfg.get("hour_end"))),
        ("INCL", bool(cfg.get("inclusive"))),
        ("AFTER_NOW", bool(cfg.get("must_be_after_now"))),
        ("GAPS", gaps),
    ]
    return _assemble("pre", consts, [_H_BASE_NODEC], _CB_PRE_BODY)


_CB_POST_BODY = """
if isinstance(result, dict):
    result.pop('header', None)
    data = result.get('data')
else:
    data = None
if isinstance(data, dict):
    state = _s(vars.get('callback_state'))
    cnt = _int(vars.get('callback_invalid_count'))
    if state == 'invalid':
        data[OK_KEY] = False
        if REASKS is not None and cnt > REASKS:
            vars['callback_state'] = 'no_valid_time'
            text = T_NONE + EXIT_NONE + '.'
        else:
            text = T_REASK + ' Allowed dates: ' + _s(vars.get('callback_dates')) + '.' + HOURS
    elif _true(data.get(OK_KEY)):
        vars['callback_state'] = 'scheduled'
        text = T_DONE + _s(vars.get('callback_sent_date')) + ' at ' + _s(vars.get('callback_sent_time')) + T_DONE_EXIT + EXIT_SCHED + '.'
    else:
        text = T_FAIL
    data['what_to_do_next'] = text
"""


def callback_post_hook(cfg: dict) -> str:
    """Post-hook of the callback tool.

    cfg keys:
      ok_key: response key (default 'ok');
      reasks: int | None, re-asks allowed after a rejected time (third_party.reasks_when_outside);
              None = the spec is silent, the hook never moves to no_valid_time by itself;
      exit_scheduled, exit_no_time: names of the transition tools to call;
      hour_start, hour_end: strings used in the re-ask text;
      texts: optional overrides.
    Rejected (callback_state 'invalid' from the pre-hook) -> data[ok_key]=False and re-ask text;
    when callback_invalid_count > reasks -> callback_state 'no_valid_time'. Accepted ->
    callback_state 'scheduled'.
    """
    t = _texts(cfg)
    hours = ""
    if cfg.get("hour_start") and cfg.get("hour_end"):
        hours = " Allowed hours: " + str(cfg["hour_start"]) + " to " + str(cfg["hour_end"]) + "."
    consts = [
        ("OK_KEY", _ok_key(cfg)),
        ("REASKS", cfg.get("reasks")),
        ("EXIT_SCHED", str(cfg.get("exit_scheduled") or "")),
        ("EXIT_NONE", str(cfg.get("exit_no_time") or "")),
        ("HOURS", hours),
        ("T_REASK", t["cb_reask"]),
        ("T_NONE", t["cb_none"]),
        ("T_DONE", t["cb_done"]),
        ("T_DONE_EXIT", t["cb_done_exit"]),
        ("T_FAIL", t["cb_fail"]),
    ]
    return _assemble("post", consts, [_H_BASE_NODEC], _CB_POST_BODY)


# ---------------------------------------------------------------------------------------------
# Promise
# ---------------------------------------------------------------------------------------------

_PROMISE_PRE_BODY = """
body = config.get('body')
if isinstance(body, dict):
    k = _pick(body.get(F_PROD))
    if k == 0:
        vars['promise_guard'] = 'skipped'
    else:
        vars['promise_idx'] = k
        pid = _s(_get(k, 'id'))
        if pid != '':
            body[F_PROD] = pid
        asked_date = _s(body.get(F_DATE)).strip()[:10]
        asked_amt = _num(body.get(F_AMT))
        vars['promise_asked_date'] = asked_date
        if asked_amt is None:
            vars['promise_asked_amount'] = ''
        else:
            vars['promise_asked_amount'] = asked_amt
        guard = ''
        dl = _s(_get(k, 'deadline'))
        cands = []
        if dl != '':
            for x in _s(vars.get('business_days')).split(','):
                if x != '' and x <= dl:
                    cands.append(x)
        if len(cands) > 0:
            if _valid_date(asked_date):
                pick = cands[-1]
                for c in cands:
                    if c >= asked_date:
                        pick = c
                        break
            else:
                pick = cands[0]
            body[F_DATE] = pick
        else:
            guard = 'skipped'
        full = _num(_get(k, 'full'))
        floor = _num(_get(k, 'floor'))
        if full is not None:
            fc = int(round(full * 100))
            out = full
            if asked_amt is not None and int(round(asked_amt * 100)) > 0:
                ac = int(round(asked_amt * 100))
                if ac >= fc:
                    out = full
                elif floor is None:
                    out = full
                elif ac < int(round(floor * 100)):
                    out = floor
                else:
                    out = asked_amt
            body[F_AMT] = round(out, DEC)
        else:
            guard = 'skipped'
        if guard != '':
            vars['promise_guard'] = guard
        sent_date = _s(body.get(F_DATE)).strip()[:10]
        sent_amt = _num(body.get(F_AMT))
        vars['promise_sent_date'] = sent_date
        if sent_amt is None:
            vars['promise_sent_amount'] = ''
        else:
            vars['promise_sent_amount'] = sent_amt
        changed = sent_date != asked_date
        if sent_amt is None or asked_amt is None or abs(sent_amt - asked_amt) >= 0.005:
            changed = True
        if changed:
            vars['promise_changed'] = 'yes'
        else:
            vars['promise_changed'] = 'no'
"""


def _slot_consts(cfg: dict) -> list[tuple[str, Any]]:
    return [
        ("N", max(1, int(cfg.get("n_products") or 1))),
        ("MULTI", bool(cfg.get("multi"))),
        ("DEC", int(cfg.get("decimals") if cfg.get("decimals") is not None else 2)),
    ]


def promise_pre_hook(cfg: dict) -> str:
    """Pre-hook of the promise tool: force the product id, clamp the date and the amount.

    cfg keys:
      fields: dict product / amount / date -> body field names;
      n_products: int (multi_product.max_products, 1 for a single product);
      multi: bool (multi_product.enabled);
      decimals: int (amounts.rounding_decimals).
    Reads vars p<k>_id / _full / _floor / _deadline / _promise / _note and business_days.
    Date: first business day >= the requested ISO date and <= the product's deadline, else the
    last such day; invalid or past -> the first. Amount: > full -> full, < floor -> floor,
    unreadable -> full, floor unknown and below full -> full. When the deadline (or the full
    amount) is unknown the part stays unchanged and vars['promise_guard']='skipped'.
    Writes promise_idx, promise_asked_date, promise_asked_amount, promise_sent_date,
    promise_sent_amount, promise_changed ('yes'|'no').
    """
    f = cfg.get("fields") or {}
    consts = _slot_consts(cfg) + [
        ("F_PROD", str(f.get("product") or "product_id")),
        ("F_AMT", str(f.get("amount") or "amount")),
        ("F_DATE", str(f.get("date") or "date")),
    ]
    return _assemble("pre", consts, [_H_BASE, _H_DATE, _H_ACTIVE, _H_PICK], _PROMISE_PRE_BODY)


_PROMISE_POST_BODY = """
if isinstance(result, dict):
    result.pop('header', None)
    data = result.get('data')
else:
    data = None
if isinstance(data, dict):
    k = _int(vars.get('promise_idx'))
    if k < 1:
        k = 1
    if _true(data.get(OK_KEY)):
        vars[_key(k, 'promise')] = 'recorded'
        vars[_key(k, 'promise_date')] = _s(vars.get('promise_sent_date'))
        vars[_key(k, 'promise_amount')] = vars.get('promise_sent_amount')
        text = T_RECORDED + _money(vars.get('promise_sent_amount')) + ' on ' + _s(vars.get('promise_sent_date')) + '.'
        if _s(vars.get('promise_changed')) == 'yes':
            text = text + ' ' + T_DIFFERS + ' Asked: ' + _money(vars.get('promise_asked_amount')) + ' on ' + _s(vars.get('promise_asked_date')) + '.'
        if SUCCESS != '':
            text = text + T_SUCCESS + SUCCESS
        text = text + _next_text(k)
    else:
        fails = _int(_get(k, 'promise_fails')) + 1
        vars[_key(k, 'promise_fails')] = fails
        allowed = 0
        if RETRIES is not None:
            allowed = RETRIES
        if fails <= allowed:
            text = T_RETRY
        elif MULTI and NOTE_REASON != '':
            vars[_key(k, 'promise')] = 'failed'
            text = T_NOTE_FOR_FAILED + NOTE_REASON + '.'
        else:
            vars['promise_state'] = 'write_failed'
            text = T_WRITE_FAILED
    data['what_to_do_next'] = text
"""


def promise_post_hook(cfg: dict) -> str:
    """Post-hook of the promise tool.

    cfg keys:
      ok_key: response key (default 'ok');
      n_products, multi, decimals: as in promise_pre_hook;
      retries: int | None (promise_rules.retries_on_failure; None counts as 0);
      note_reason: str | None (multi_product.on_promise_failure.note_reason);
      success_sentence: str | None; goodbye: str | None (closing.goodbye_before_close.text);
      finalize_transition: name of the transition tool that goes to the finalize node;
      texts: optional overrides.
    ok -> p<k>_promise 'recorded' (+ _promise_date, _promise_amount), closing_ready 'yes' once
    every product is recorded or noted; what_to_do_next states the actual recorded values.
    Not ok -> p<k>_promise_fails += 1; within retries call again; then multi + note_reason ->
    p<k>_promise 'failed' (note it), else promise_state 'write_failed'.
    """
    t = _texts(cfg)
    consts = _slot_consts(cfg) + [
        ("OK_KEY", _ok_key(cfg)),
        ("RETRIES", cfg.get("retries")),
        ("NOTE_REASON", str(cfg.get("note_reason") or "")),
        ("SUCCESS", str(cfg.get("success_sentence") or "")),
        ("FINISH", _finish_text(cfg, t)),
        ("T_RECORDED", t["recorded"]),
        ("T_DIFFERS", t["differs"]),
        ("T_SUCCESS", t["success"]),
        ("T_NEXT_PRODUCT", t["next_product"]),
        ("T_NEXT_TOOLS", t["next_product_tools"]),
        ("T_RETRY", t["retry"]),
        ("T_NOTE_FOR_FAILED", t["note_for_failed"]),
        ("T_WRITE_FAILED", t["write_failed"]),
    ]
    return _assemble("post", consts, [_H_BASE, _H_ACTIVE, _H_NEXT_TEXT], _PROMISE_POST_BODY)


# ---------------------------------------------------------------------------------------------
# Note
# ---------------------------------------------------------------------------------------------

_NOTE_PRE_BODY = """
body = config.get('body')
if isinstance(body, dict):
    k = _pick(body.get(F_PROD))
    if k == 0:
        vars['note_guard'] = 'skipped'
    else:
        vars['note_idx'] = k
        pid = _s(_get(k, 'id'))
        if pid != '':
            body[F_PROD] = pid
        reason = _s(body.get(F_REASON))
        if _s(_get(k, 'promise')) == 'failed' and FALLBACK != '':
            reason = FALLBACK
        elif len(ALLOWED) > 0 and reason not in ALLOWED:
            if FALLBACK != '':
                reason = FALLBACK
            elif len(REASONS) > 0:
                reason = REASONS[0]
        body[F_REASON] = reason
        vars['note_sent_reason'] = reason
        if _s(vars.get('notes_blocked')) == 'yes':
            vars['note_blocked_hit'] = 'yes'
"""


def note_pre_hook(cfg: dict) -> str:
    """Pre-hook of the note tool: force the product id and keep the reason inside the enum.

    cfg keys:
      fields: dict product / reason -> body field names;
      n_products, multi: as in promise_pre_hook;
      allowed_reasons: list, the openapi enum of the reason field (empty = no check);
      fallback_reason: str | None (multi_product.on_promise_failure.note_reason): used when the
        product's promise failed or the model's reason is outside the enum;
      reasons: list (multi_product.notes.reasons), the last fallback (its first entry).
    Writes note_idx, note_sent_reason; note_guard 'skipped' when no product is left;
    note_blocked_hit 'yes' when notes_blocked is 'yes' (flag only, the request still goes out).
    """
    f = cfg.get("fields") or {}
    consts = _slot_consts(cfg) + [
        ("F_PROD", str(f.get("product") or "product_id")),
        ("F_REASON", str(f.get("reason") or "reason")),
        ("ALLOWED", list(cfg.get("allowed_reasons") or [])),
        ("FALLBACK", str(cfg.get("fallback_reason") or "")),
        ("REASONS", list(cfg.get("reasons") or [])),
    ]
    return _assemble("pre", consts, [_H_BASE, _H_ACTIVE, _H_PICK], _NOTE_PRE_BODY)


_NOTE_POST_BODY = """
if isinstance(result, dict):
    result.pop('header', None)
    data = result.get('data')
else:
    data = None
if isinstance(data, dict):
    k = _int(vars.get('note_idx'))
    if k < 1:
        k = 1
    reason = _s(vars.get('note_sent_reason'))
    if _true(data.get(OK_KEY)):
        vars[_key(k, 'note')] = reason
        text = T_OK + _next_text(k)
    else:
        fails = _int(_get(k, 'note_fails')) + 1
        vars[_key(k, 'note_fails')] = fails
        if fails <= 1:
            text = T_RETRY
        else:
            vars[_key(k, 'note')] = reason
            text = T_GAVE_UP + _next_text(k)
    data['what_to_do_next'] = text
"""


def note_post_hook(cfg: dict) -> str:
    """Post-hook of the note tool.

    cfg keys: ok_key, n_products, multi, decimals, goodbye, finalize_transition, texts (see
    promise_post_hook). ok -> p<k>_note = the reason sent, closing_ready 'yes' once every
    product is recorded or noted, text = next product or finish. Not ok: one retry, then the
    note is counted as given (p<k>_note set) so the call cannot hang on it.
    """
    t = _texts(cfg)
    consts = _slot_consts(cfg) + [
        ("OK_KEY", _ok_key(cfg)),
        ("FINISH", _finish_text(cfg, t)),
        ("T_OK", t["note_ok"]),
        ("T_RETRY", t["note_retry"]),
        ("T_GAVE_UP", t["note_gave_up"]),
        ("T_NEXT_PRODUCT", t["next_product"]),
        ("T_NEXT_TOOLS", t["next_product_tools"]),
    ]
    return _assemble("post", consts, [_H_BASE, _H_ACTIVE, _H_NEXT_TEXT], _NOTE_POST_BODY)


# ---------------------------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------------------------


def transfer_pre_hook(cfg: dict) -> str:
    """Pre-hook of the transfer tool: remember the queue (the post-hook cannot see the request).

    cfg keys: fields: dict queue -> body field name. Writes vars['transfer_sent_queue'].
    The request itself is not changed.
    """
    f = cfg.get("fields") or {}
    body = """
    body = config.get('body')
    if isinstance(body, dict):
        vars['transfer_sent_queue'] = _s(body.get(F_QUEUE))
    """
    return _assemble(
        "pre",
        [("F_QUEUE", str(f.get("queue") or "queue"))],
        [_H_BASE_NODEC],
        body,
    )


_TRANSFER_POST_BODY = """
if isinstance(result, dict):
    result.pop('header', None)
    data = result.get('data')
else:
    data = None
if isinstance(data, dict):
    if _true(data.get(OK_KEY)):
        queue = _s(vars.get('transfer_sent_queue'))
        reason = queue
        for pair in QUEUE_REASONS:
            if len(pair) == 2 and _s(pair[0]) == queue:
                reason = _s(pair[1])
                break
        vars['transfer_state'] = 'done'
        vars['transfer_reason'] = reason
        vars['closing_ready'] = 'yes'
        if NO_NOTES:
            vars['notes_blocked'] = 'yes'
        text = T_OK + ' ' + FINISH
    else:
        text = T_FAIL
    data['what_to_do_next'] = text
"""


def transfer_post_hook(cfg: dict) -> str:
    """Post-hook of the transfer tool.

    cfg keys:
      ok_key; queue_reasons: list of [queue, reason] pairs (queue -> closing reason; a queue
        without a pair uses the queue name as reason); no_notes: bool
        (multi_product.on_transfer.add_no_notes); goodbye; finalize_transition; texts.
    ok -> transfer_state 'done', transfer_reason, closing_ready 'yes', notes_blocked 'yes' when
    no_notes. Needs vars['transfer_sent_queue'] (see transfer_pre_hook).
    """
    t = _texts(cfg)
    consts = [
        ("OK_KEY", _ok_key(cfg)),
        ("QUEUE_REASONS", [list(p) for p in (cfg.get("queue_reasons") or [])]),
        ("NO_NOTES", bool(cfg.get("no_notes"))),
        ("FINISH", _finish_text(cfg, t)),
        ("T_OK", t["transfer_ok"]),
        ("T_FAIL", t["transfer_fail"]),
    ]
    return _assemble("post", consts, [_H_BASE_NODEC], _TRANSFER_POST_BODY)


# ---------------------------------------------------------------------------------------------
# Final outcome (close pre-hook and finalize node share the block)
# ---------------------------------------------------------------------------------------------

_H_FINAL = """
def _pair(fb):
    if fb is None:
        return None
    return (_s(fb.get('outcome')), _s(fb.get('reason')))
def _fallback():
    if _s(vars.get('identity_state')) == 'verified':
        return _pair(FB_POST)
    return _pair(FB_PRE)
def _final_pair():
    out = _s(vars.get('final_outcome'))
    if out != '':
        pair = (out, _s(vars.get('final_reason')))
    else:
        tr = _s(vars.get('transfer_reason'))
        pair = None
        if _s(vars.get('identity_state')) != 'verified' and tr == '' and FB_PRE is not None:
            pair = _pair(FB_PRE)
        else:
            states = []
            for k in _active():
                note = _s(_get(k, 'note'))
                if note == '':
                    note = None
                states.append(dict(product_id=_get(k, 'id'), promise_recorded=_s(_get(k, 'promise')) == 'recorded', note_reason=note))
            if tr == '':
                tr = None
            res = aggregate_outcome(states, AGG_SPEC, tr)
            if _s(res.get('outcome')) != '':
                pair = (_s(res.get('outcome')), _s(res.get('reason')))
            else:
                pair = _fallback()
    if pair is not None and pair[0] in REASONLESS:
        pair = (pair[0], '')
    return pair
"""


def _final_parts(cfg: dict) -> tuple[list[tuple[str, Any]], list[str], list[tuple[str, Any]]]:
    agg = cfg.get("aggregation")
    consts = [
        ("N", max(1, int(cfg.get("n_products") or 1))),
        ("AGG_SPEC", {"closing": {"aggregation": agg if isinstance(agg, dict) else {}}}),
        ("FB_PRE", cfg.get("fallback_pre")),
        ("FB_POST", cfg.get("fallback_post")),
        ("REASONLESS", list(cfg.get("reasonless") or [])),
    ]
    helpers = [_H_BASE_NODEC, _H_ACTIVE, engine_function_source("aggregate_outcome"), _H_FINAL]
    top = [("_FB_PRE", cfg.get("fallback_pre")), ("_FB_POST", cfg.get("fallback_post"))]
    return consts, helpers, top


_CLOSE_PRE_BODY = """
body = config.get('body')
if isinstance(body, dict):
    pair = _final_pair()
    if pair is not None:
        body[F_OUT] = pair[0]
        if pair[1] != '':
            body[F_RSN] = pair[1]
        else:
            body.pop(F_RSN, None)
"""

_CLOSE_PRE_FALLBACK = """
try:
    _fb = _FB_POST if str(vars.get('identity_state') or '') == 'verified' else _FB_PRE
    if _fb is not None:
        config['body'][%s] = _fb['outcome']
        if _fb.get('reason'):
            config['body'][%s] = _fb['reason']
        else:
            config['body'].pop(%s, None)
except Exception:
    pass
"""


def close_pre_hook(cfg: dict) -> str:
    """Pre-hook of the close tool (a lifecycle tool): write the final outcome and reason.

    cfg keys:
      fields: dict outcome / reason -> body field names;
      aggregation: spec closing.aggregation dict (embedded; aggregate_outcome runs on it);
      n_products: int; fallback_pre, fallback_post: {outcome, reason} | None, the pair used when
        the aggregation gives nothing (pre = identity not verified, post = verified);
      reasonless: outcomes that never carry a reason.
    Uses vars final_outcome / final_reason when set; else, for an unverified call without a
    transfer, the pre fallback; else aggregate_outcome over p<k>_promise / p<k>_note with
    vars['transfer_reason']; an empty outcome falls back to the pair for the identity state.
    On any exception the fallback pair is written. The reason key is removed when empty.
    """
    f = cfg.get("fields") or {}
    consts, helpers, top = _final_parts(cfg)
    f_out = str(f.get("outcome") or "outcome")
    f_rsn = str(f.get("reason") or "reason")
    consts = consts + [("F_OUT", f_out), ("F_RSN", f_rsn)]
    fallback = _CLOSE_PRE_FALLBACK % (literal(f_out), literal(f_rsn), literal(f_rsn))
    return _assemble("pre", consts, helpers, _CLOSE_PRE_BODY, fallback=fallback, top=top)


_FINALIZE_BODY = """
if FORCE:
    vars['closing_ready'] = 'yes'
if _s(vars.get('closing_ready')) == 'yes':
    pair = _final_pair()
    if pair is not None:
        vars['final_outcome'] = pair[0]
        vars['final_reason'] = pair[1]
"""

_FINALIZE_FALLBACK = """
try:
    vars['closing_ready'] = 'yes'
    _fb = _FB_POST if str(vars.get('identity_state') or '') == 'verified' else _FB_PRE
    if _fb is not None:
        vars['final_outcome'] = _fb['outcome']
        vars['final_reason'] = _fb.get('reason') or ''
except Exception:
    pass
"""


def finalize_source(cfg: dict, force: bool) -> str:
    """Code-node source computing final_outcome / final_reason once the call is ready to end.

    cfg keys: aggregation, n_products, fallback_pre, fallback_post, reasonless (see
    close_pre_hook). force=False does nothing unless vars['closing_ready'] == 'yes'; force=True
    sets it first. On any exception: closing_ready 'yes' and the fallback pair.
    """
    consts, helpers, top = _final_parts(cfg)
    consts = consts + [("FORCE", bool(force))]
    return _assemble("code", consts, helpers, _FINALIZE_BODY, fallback=_FINALIZE_FALLBACK, top=top)


def exit_source(outcome: str, reason: str | None, require: tuple[str, str] | None = None) -> str:
    """Code-node source for an exit path: set final_outcome (and final_reason when it has one).

    require=(variable, value): only write when vars[variable] equals value (used by the gated
    callback exit, which routes back when the callback was not scheduled).
    """
    lines = ["vars['final_outcome'] = " + literal(str(outcome))]
    if reason:
        lines.append("vars['final_reason'] = " + literal(str(reason)))
    if require is not None:
        head = "if str(vars.get(" + literal(require[0]) + ") or '') == " + literal(require[1]) + ":"
        lines = [head] + ["    " + x for x in lines]
    source = "try:\n" + textwrap.indent("\n".join(lines), "    ") + "\nexcept Exception:\n    pass\n"
    return check_source(source)
