"""No-network syntax/logic checks for the source file."""
from pathlib import Path
import ast
import ipaddress
import re
from urllib.parse import urlparse

src = Path(__file__).resolve().parents[1] / "bot.py"
tree = ast.parse(src.read_text(encoding="utf-8"))
functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
ns = {"re": re, "ipaddress": ipaddress, "urlparse": urlparse}
for name in ("valid_public_https_url", "normalize_phone", "task_key"):
    exec(compile(ast.Module(body=[functions[name]], type_ignores=[]), str(src), "exec"), ns)
assert ns["normalize_phone"]("08012345678") == "+2348012345678"
assert ns["normalize_phone"]("+2348012345678") == "+2348012345678"
assert ns["normalize_phone"]("000") is None
assert ns["task_key"]("MTN", 1, "URL") == "URL_MTN"
assert ns["task_key"]("MTN", 2, "URL") == "URL_MTN_2"
assert ns["task_key"]("GLO", 2, "BILLING") == "BILLING_GLO_2"
assert ns["valid_public_https_url"]("https://example.com/offer")
assert not ns["valid_public_https_url"]("http://example.com")
assert not ns["valid_public_https_url"]("https://127.0.0.1/foo")
assert not ns["valid_public_https_url"]("https://localhost/foo")
print("Static and pure-function tests passed")

# Commands (including /admin) must bypass the generic text handler.
assert '@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))' in src.read_text(encoding="utf-8")
