"""The problem inbox: send Hao a short report when something goes wrong (`ledgerctl.py vault report`).

A report has four fields: step (where it broke), summary (one line, in the user's words), error (the error
text) and checks (what tools/report.py found on this machine). Before anything is sent, `clean` takes out what
must never leave: keys, invite and recovery codes, long hex strings, amounts, email addresses and the home
folder's path. The server refuses a report that still looks like it holds a key.

Error text is cut down further, to the program's own words (`skeleton`): only phrases that appear in Acost's
own source code, error codes, exception names and HTTP statuses are kept; everything put into a message from
the ledger (shop names, dates, categories, notes, ids) becomes "…". So whatever Muse pastes into "error", no
entry can leave in it. The summary is the user's own sentence and is sent as written (Muse is told to keep
shop names and personal details out of it).

Needs only the standard library, so it works even when the install broke before `cryptography` was set up.
With a ledger key the report is filed under that ledger; with no key yet, the invite code is the pass.
"""
import ast
import json
from pathlib import Path
import re
import time

from .admin import SERVICE_URL
from .sync import Transport, VaultError

STEPS = ("install", "record", "dashboard", "daily", "update", "other")
MAX_SUMMARY, MAX_ERROR, MAX_CHECKS = 300, 2000, 5000

_HIDE = (
    (re.compile(r"ql_[A-Za-z0-9_.-]+"), "[钥匙]"),
    (re.compile(r"ACOST1(?:-[A-Za-z2-7]{4})+", re.I), "[恢复码]"),
    (re.compile(r"ACOST(?:[-\s]?[0-9a-f]{4}){5}", re.I), "[邀请码]"),
    (re.compile(r"sb_(?:secret|publishable)_[A-Za-z0-9_-]+|eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+){1,2}"), "[钥匙]"),
    (re.compile(r"(?i)\bbearer\s+[^\s\",}]+"), "Bearer [隐去]"),
    (re.compile(r"(?i)(authorization|token|apikey|api_key|password|secret)(\"?\s*[:=]\s*\"?)[^\s\",}]+"), r"\1\2[隐去]"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[长串]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[邮箱]"),
    (re.compile(r"(\w*_cents\"?\s*[:=]\s*)-?\d+"), r"\1[金额]"),
    (re.compile(r"(?i)(?:-|−)?(?:C\$|CA\$|CAD\s?|\$|¥|￥)\s?\d[\d,]*(?:\.\d+)?"
                r"|(?:-|−)?(?<![\d.])\d[\d,]*\.\d{2}(?![\d.])(?!\s?(?:秒|s\b|ms\b))(?:\s?(?:元|加元|CAD))?"), "[金额]"),
)


def scrub(text):
    text = str(text).replace(str(Path.home()), "~")
    for pattern, replacement in _HIDE:
        text = pattern.sub(replacement, text)
    return text


def _scrub_value(value, depth=0):
    if depth > 4:
        return "…"
    if isinstance(value, str):
        return scrub(value)[:500]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item, depth + 1) for item in list(value)[:40]]
    if isinstance(value, dict):
        return {scrub(key)[:60]: _scrub_value(item, depth + 1) for key, item in list(value.items())[:40]}
    return scrub(repr(value))[:200]


ROOT = Path(__file__).resolve().parent.parent
# Words that are safe whatever their source: Python exception names, HTTP statuses, error codes such as
# possible_duplicate, JSON keys, true/false/null, a traceback's file names and line numbers.
_SAFE = (r"\b[A-Z][A-Za-z]*(?:Error|Exception|Disconnected|Timeout|Refused|Reset|Interrupt|Exit)\b",
         r"\bHTTP \d{3}\b", r"\b[a-z]+(?:_[a-z]+)+\b", r'"[a-z_]+"(?=\s*:)', r"\b(?:true|false|null)\b",
         r"\b[\w-]+\.py\b", r"\bline \d+\b", r"\[(?:钥匙|恢复码|邀请码|隐去|长串|邮箱|金额)\]")
_PLACEHOLDER = re.compile(r"%(?:\(\w+\))?[-+ #0]*\d*(?:\.\d+)?[sdrfxi%]|\{[^{}]*\}")
_CJK = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")
_GAP = re.compile(r"[^\w]*")      # spaces and punctuation between two kept pieces, no letters or digits
_skeleton = None


def _program_phrases(root=ROOT):
    """Every fixed piece of text in Acost's Python source (not docstrings), split where values are put in."""
    phrases = set()
    for path in sorted(list(root.glob("*.py")) + list(root.glob("acost_vault/*.py")) + list(root.glob("tools/*.py"))):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                      and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                for piece in _PLACEHOLDER.split(node.value):
                    bare = piece.strip()
                    # Chinese wording, or an English phrase; never one bare word, which could be a name
                    if (_CJK.search(bare) and len(bare) >= 2) or (len(bare) >= 8 and re.search(r"[\s:;,.!?()'\"]", bare)):
                        phrases.add(piece)
    return phrases


def skeleton(text, root=None):
    """The text with only the program's own words left; what was put into them becomes "…"."""
    global _skeleton
    if root is not None or _skeleton is None:
        phrases = sorted(_program_phrases(root or ROOT), key=len, reverse=True)
        pattern = re.compile("|".join([re.escape(phrase) for phrase in phrases] + list(_SAFE)))
        if root is not None:
            return _cut(pattern, text)
        _skeleton = pattern
    return _cut(_skeleton, text)


def _cut(pattern, text):
    kept, last = [], 0
    for match in pattern.finditer(text):
        if match.start() > last:
            gap = text[last:match.start()]
            kept.append(gap if _GAP.fullmatch(gap) else "…")
        kept.append(match.group())
        last = match.end()
    if last < len(text):
        kept.append("…")
    return re.sub(r"…(?:\s*…)+", "…", "".join(kept)).strip()


def _skeleton_errors(value):
    """Inside the checks, error texts get the same treatment as the error field."""
    if isinstance(value, dict):
        return {key: skeleton(item) if key == "error" and isinstance(item, str) else _skeleton_errors(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_skeleton_errors(item) for item in value]
    return value


def clean(report):
    """The report as it will be sent: only the four fields, scrubbed and cut to size."""
    if not isinstance(report, dict):
        raise VaultError("报告必须是 JSON 对象（step、summary、error、checks）。", 400)
    unknown = set(report) - {"step", "summary", "error", "checks"}
    if unknown:
        raise VaultError("报告只接受 step、summary、error、checks，多了：" + "、".join(sorted(unknown)), 400)
    step = report.get("step") or "other"
    if step not in STEPS:
        raise VaultError("step 只能是 " + "、".join(STEPS), 400)
    summary = scrub(report.get("summary") or "").strip()[:MAX_SUMMARY]
    if not summary:
        raise VaultError("summary 不能为空：用一句话说出了什么问题。", 400)
    error = skeleton(scrub(report.get("error") or ""))
    if len(error) > MAX_ERROR:      # the end of an error (the last lines of a traceback) says the most
        error = "…" + error[-(MAX_ERROR - 1):]
    checks = report.get("checks") or {}
    if not isinstance(checks, dict):
        raise VaultError("checks 必须是 JSON 对象。", 400)
    checks = _skeleton_errors(_scrub_value(checks))
    while len(json.dumps(checks, ensure_ascii=False)) > MAX_CHECKS and checks:
        checks.pop(max(checks, key=lambda key: len(json.dumps(checks[key], ensure_ascii=False))))
        checks["cut"] = True
    return {"step": step, "summary": summary, "error": error, "checks": checks}


def send(report, *, token=None, invite=None, url=SERVICE_URL, use_env_proxy=False, tries=3, sleep=time.sleep):
    """File the report; returns the server's answer ({report_id, message}). A dropped connection is tried again:
    a report sent twice is harmless (Hao sees it twice), a lost one is not."""
    body = clean(report)
    if not token:
        if not invite:
            raise VaultError("还没有钥匙：要带上邀请码（--invite）才能发报告。", 400)
        body["invite"] = str(invite).strip()
    link = Transport(url, token, use_env_proxy=use_env_proxy)
    for attempt in range(tries):
        if attempt:
            sleep(3 * attempt)
        try:
            status, answer = link.call("POST", "/api/report", body)
        except VaultError as error:
            if attempt + 1 == tries:
                raise
            continue
        if status == 201:
            sent = dict(body)
            sent.pop("invite", None)
            return {"ok": True, "report_id": answer.get("report_id"), "message": answer.get("message"), "sent": sent}
        if status < 500:
            raise VaultError("HTTP %d：%s" % (status, answer.get("message", "报告没有发出")), status)
    raise VaultError("HTTP %d：%s" % (status, answer.get("message", "报告没有发出")), status)
