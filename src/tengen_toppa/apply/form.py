"""Generic DOM form discovery + filling. Works on any Playwright Page (Greenhouse, Lever, Ashby, others)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Page

from tengen_toppa.apply.fields import FormField, normalize_question

log = logging.getLogger(__name__)

# Tags every fillable control with data-tt-id and returns a description of each question.
# Radio/checkbox groups become one field with options; option inputs get data-tt-opt.
_COLLECT_JS = r"""
(rootSel) => {
  const ROOT = rootSel ? (document.querySelector(rootSel) || document) : document;
  const txt = el => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const shown = el => { if (!el) return false; const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const usable = el => el.type === 'file' || shown(el) || (el.labels && Array.from(el.labels).some(shown));
  const byFor = el => el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
  const CONTAINER = 'fieldset, .field, .application-question, [class*="question"], [class*="field"], [role="group"], [role="radiogroup"], li';
  const containerLabel = el => {
    let c = el.parentElement ? el.parentElement.closest(CONTAINER) : null;
    for (let depth = 0; c && depth < 4; depth++, c = c.parentElement ? c.parentElement.closest(CONTAINER) : null) {
      const lg = c.querySelector('legend'); if (lg && txt(lg)) return txt(lg);
      const l = Array.from(c.querySelectorAll('label, .label, .text, [class*="label"], [class*="title"]'))
        .find(x => !x.querySelector('input, select, textarea') && txt(x));
      if (l) return txt(l);
    }
    return '';
  };
  const labelOf = el => {
    let t = txt(byFor(el));
    if (!t && el.getAttribute('aria-labelledby'))
      t = el.getAttribute('aria-labelledby').split(/\s+/).map(id => txt(document.getElementById(id))).join(' ').trim();
    if (!t) t = (el.getAttribute('aria-label') || '').trim();
    if (!t) { const l = el.closest('label'); if (l) t = txt(l); }
    if (!t) t = containerLabel(el);
    if (!t) t = el.getAttribute('placeholder') || el.name || '';
    return t;
  };
  const out = []; const groups = {}; let n = 0;
  for (const el of ROOT.querySelectorAll('input, textarea, select')) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search', 'password'].includes(type)) continue;
    if (el.disabled || el.readOnly || !usable(el)) continue;
    const req = el.required || el.getAttribute('aria-required') === 'true';
    const isGroup = type === 'radio' || (type === 'checkbox' && el.name &&
      document.querySelectorAll(`input[type=checkbox][name="${CSS.escape(el.name)}"]`).length > 1);
    if (isGroup) {
      const g = el.name || ('_g' + n);
      const opt = txt(byFor(el)) || txt(el.closest('label')) || el.value;
      if (!groups[g]) {
        const q = containerLabel(el) || g;
        groups[g] = { key: 'tt' + (n++), label: q, kind: type, required: req || /\*/.test(q), options: [], name: el.name || '', value: '' };
        out.push(groups[g]);
      }
      el.setAttribute('data-tt-id', groups[g].key); el.setAttribute('data-tt-opt', opt);
      groups[g].options.push(opt); groups[g].required = groups[g].required || req;
      continue;
    }
    const key = 'tt' + (n++);
    el.setAttribute('data-tt-id', key);
    const tag = el.tagName.toLowerCase();
    let kind = tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : (el.getAttribute('role') === 'combobox' ? 'combobox' : type);
    if (!['text','email','tel','url','number','textarea','select','combobox','checkbox','file','date'].includes(kind)) kind = 'text';
    const label = labelOf(el);
    const options = kind === 'select' ? Array.from(el.options).map(o => o.text.trim()).filter(Boolean) : [];
    out.push({ key, label, kind, required: req || /\*\s*$/.test(label) || /\*/.test(label.slice(-3)), options,
               name: el.name || '', value: kind === 'file' ? '' : (el.value || '') });
  }
  return out;
}
"""

_CAPTCHA_JS = r"""
() => Array.from(document.querySelectorAll('iframe')).some(f => {
  const src = f.src || ''; if (!/recaptcha|hcaptcha|challenges\.cloudflare|turnstile|arkoselabs|funcaptcha/.test(src)) return false;
  if (f.closest('.grecaptcha-badge')) return false;  // invisible reCAPTCHA v3 badge is not a challenge
  if (/size=invisible/.test(src)) return false;
  const r = f.getBoundingClientRect(); return r.width >= 60 && r.height >= 60;
})
"""


def collect_fields(page: Page, root: str | None = None) -> list[FormField]:
    raw = page.evaluate(_COLLECT_JS, root)
    fields = [
        FormField(**{k: r[k] for k in ("key", "label", "kind", "required", "options", "name", "value")}) for r in raw
    ]
    for f in fields:
        f.label = re.sub(r"\s*\*\s*$", "", f.label).strip()
    return fields


def captcha_visible(page: Page) -> bool:
    return bool(page.evaluate(_CAPTCHA_JS))


def file_role(f: FormField) -> str | None:
    n = normalize_question(f"{f.label} {f.name}")
    if re.search(r"\bcover\b", n):
        return "cover_letter"
    if re.search(r"\b(resume|cv|curriculum)\b", n):
        return "resume"
    return None


@dataclass
class FillReport:
    filled: dict[str, str] = field(default_factory=dict)  # label -> value
    uploaded: dict[str, str] = field(default_factory=dict)  # label -> filename
    skipped_optional: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def fill_form(
    page: Page, fields: list[FormField], answers: dict[str, str], *, resume: Path, cover_letter: Path | None = None
) -> FillReport:
    report = FillReport()
    file_fields = [f for f in fields if f.kind == "file"]
    for f in file_fields:
        role = file_role(f) or ("resume" if len(file_fields) == 1 else None)
        path = resume if role == "resume" else cover_letter if role == "cover_letter" else None
        if path is None:
            continue
        try:
            page.locator(f'[data-tt-id="{f.key}"]').set_input_files(str(path))
            report.uploaded[f.label or role or "file"] = path.name
        except Exception as e:
            report.errors.append(f"upload '{f.label}': {e}")

    for f in fields:
        if f.kind == "file":
            continue
        value = answers.get(f.key)
        if not value:
            if not f.required:
                report.skipped_optional.append(f.label)
            continue
        try:
            _fill_one(page, f, value)
            report.filled[f.label] = value
        except Exception as e:
            report.errors.append(f"'{f.label}': {str(e).splitlines()[0][:200]}")
    return report


def _fill_one(page: Page, f: FormField, value: str) -> None:
    loc = page.locator(f'[data-tt-id="{f.key}"]')
    match f.kind:
        case "select":
            loc.first.select_option(label=value)
        case "combobox":
            box = loc.first
            box.click()
            box.fill(value)
            page.wait_for_timeout(500)
            page.keyboard.press("Enter")
        case "radio" | "checkbox" if f.options:
            wanted = {v.strip() for v in value.split("|")} if f.kind == "checkbox" else {value}
            for el in loc.all():
                if el.get_attribute("data-tt-opt") in wanted and not el.is_checked():
                    el.evaluate("e => e.click()")
        case "checkbox":
            if value in ("check", "Yes") and not loc.first.is_checked():
                loc.first.evaluate("e => e.click()")
        case _:
            loc.first.fill(value)


def click_first(page: Page, selectors: tuple[str, ...], timeout: int = 4000) -> bool:
    for sel in selectors:
        loc = page.locator(sel)
        try:
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


def page_text(page: Page) -> str:
    try:
        return page.inner_text("body", timeout=5000)
    except Exception:
        return ""
