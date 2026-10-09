"""Site navigation of a web guide: is the chrome around the content right?

The content checks read only the page's main content (nav, header and footer are
excluded). This module reads that chrome on every captured page and validates it:

  left navigation   every level-1 entry of the PDF's TOC is in the left navigation, at the top
                    level and in the same order; every navigation link opens the right page
                    (HTTP status, page title/heading = link text, #fragment exists); the
                    highlighted entry is the page itself; the navigation is the same on every page
  download PDF      the button is there on every page, the file downloads, is a PDF, and is the
                    same document as prod (page count, text)
  next / previous   each page's next / previous topic is the next / previous page of the
                    navigation, its label names that page, the first page has no previous
                    and the last no next
  on this page      the right-hand list names every sub-heading of the page, in order, and each
                    entry jumps to its heading
  product subtitle  the product title in the header: which element it is read from, the AEM
                    property it comes from (author instance), the same on every page, and whether
                    it names the product of the prod PDF (cover / title); the version in the footer

Everything is found by heuristics (labels such as "Table of contents", "On this page",
"Next topic", "Download PDF"); `[site]` in the config takes CSS selectors when a site
needs them.
"""
from __future__ import annotations

import csv
import re
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import normalize

# Runs in the page: the chrome around the content.
CHROME_JS = r"""
([sel, rootSel]) => {
  const root = (rootSel && document.querySelector(rootSel)) || document.querySelector('main') ||
               document.querySelector('article') || document.body;
  const W = document.documentElement.clientWidth || 1280;
  // some components (icon/illustration widgets especially) render their real <img>/<svg> inside a
  // shadow root, which plain querySelectorAll never sees - walk open shadow roots too so pictures
  // placed that way are still found, instead of silently reporting "0 picture(s) checked"
  const deepQueryAll = (node, selector) => {
    const out = [...node.querySelectorAll(selector)];
    for (const el of node.querySelectorAll('*')) {
      if (el.shadowRoot) out.push(...deepQueryAll(el.shadowRoot, selector));
    }
    return out;
  };
  const txt = el => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const visible = el => !!el && (el.checkVisibility ? el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) : el.offsetParent !== null);
  const box = el => { const r = el.getBoundingClientRect(); return [Math.round(r.left + scrollX), Math.round(r.top + scrollY), Math.round(r.right + scrollX), Math.round(r.bottom + scrollY)]; };
  const path = el => {  // short CSS path, so the report can say where a value is read from
    const out = [];
    for (let e = el; e && e.nodeType === 1 && out.length < 5; e = e.parentElement) {
      if (e.id) { out.unshift(e.tagName.toLowerCase() + '#' + CSS.escape(e.id)); break; }
      const cls = [...e.classList].filter(c => !/^(is-|has-|active|current|selected|open)/.test(c)).slice(0, 2);
      out.unshift(e.tagName.toLowerCase() + cls.map(c => '.' + CSS.escape(c)).join(''));
      if (e === document.body) break;
    }
    return out.join(' > ');
  };
  const q = s => { try { return s ? document.querySelector(s) : null; } catch (e) { return null; } };
  const qa = s => { try { return s ? [...document.querySelectorAll(s)] : []; } catch (e) { return []; } };
  // the smallest element around a label that holds at least `min` links
  const panelOf = (label, min) => { for (let e = label; e && e !== document.body; e = e.parentElement) if (e.querySelectorAll('a[href]').length >= min) return e; return null; };
  const labelled = re => qa('h1,h2,h3,h4,h5,h6,p,span,div,strong,b,button,summary,label,[aria-label]').find(e =>
    (re.test(txt(e)) && txt(e).length < 40 && !e.querySelector('a[href]') && e.children.length <= 2) || re.test(e.getAttribute('aria-label') || ''));
  const ACTIVE = /(^|[-_\s])(active|current|selected|is-active|is-current)($|[-_\s])/i;
  const items = (panel, skipSel) => {
    if (!panel) return [];
    const links = [...panel.querySelectorAll('a[href]')].filter(a => !(skipSel && a.closest(skipSel)) && txt(a));
    const lists = links.map(a => { let n = 0; for (let e = a.parentElement; e && e !== panel; e = e.parentElement) if (/^(UL|OL)$/.test(e.tagName)) n++; return n; });
    const minList = Math.min(...lists, 99);
    const xs = links.map(a => box(a)[0]), minX = Math.min(...xs, 1e9);
    return links.map((a, k) => {
      const li = a.closest('li');
      const active = a.getAttribute('aria-current') && a.getAttribute('aria-current') !== 'false' ||
                     ACTIVE.test(a.className || '') || (li && ACTIVE.test(li.className || '') && li.querySelector('a[href]') === a);
      const depth = minList < 99 && lists.some(n => n !== minList) ? lists[k] - minList : Math.round((xs[k] - minX) / 12) > 0 ? 1 : 0;
      return { text: txt(a), href: a.href, raw: a.getAttribute('href'), depth, active: !!active, visible: visible(a), box: box(a), where: path(a) };
    });
  };
  // left navigation (table of contents)
  let nav = q(sel.nav), navLabel = '';
  if (!nav) {
    const l = labelled(/^(table of )?contents$|^in this (guide|manual)$/i);
    if (l) { nav = panelOf(l, 2); navLabel = txt(l); }
  }
  if (!nav) {
    const cands = qa('nav, aside, [role="navigation"]').filter(e => { const b = box(e); return (b[0] + b[2]) / 2 < W * 0.35 && e.querySelectorAll('a[href]').length >= 2; });
    nav = cands.sort((a, b) => b.querySelectorAll('a[href]').length - a.querySelectorAll('a[href]').length)[0] || null;
  }
  // on this page (right side)
  let otp = q(sel.on_this_page), otpLabel = '';
  if (!otp) {
    const l = labelled(/^(on this page|in this (topic|article|page|section)|page contents|contents on this page)$/i);
    if (l) { otp = panelOf(l, 1); otpLabel = txt(l); }
  }
  if (!otp) {
    const here = location.href.split('#')[0];
    const cands = qa('nav, aside, [role="navigation"], div').filter(e => { const b = box(e); const a = [...e.querySelectorAll('a[href]')];
      return (b[0] + b[2]) / 2 > W * 0.65 && a.length >= 1 && a.every(x => x.href.split('#')[0] === here && x.href.includes('#')); });
    otp = cands.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length)[0] || null;
  }
  if (nav && otp && (nav.contains(otp) || otp.contains(nav))) otp = null;
  const panels = [nav, otp].filter(Boolean);
  const inPanels = el => panels.some(p => p.contains(el));
  // next / previous topic. pagerBars: each link's own small wrapping container, excluded from content
  // scans below only once BOTH next and prev are found - excluding it mid-search would also hide
  // whichever of the two is searched second, since they usually share the same bar
  const pagerBars = [];
  const pager = (s, re) => {
    let a = q(s);
    if (!a) {
      const c = qa('a[href], button, [role="link"]').filter(e => !inPanels(e) && (re.test(txt(e)) || re.test(e.getAttribute('aria-label') || '') ||
                re.test(e.getAttribute('rel') || '') || re.test(e.getAttribute('title') || '')));
      a = c[c.length - 1] || null;  // the pager at the end of the topic, not a carousel arrow on top
    }
    if (!a) return null;
    const link = a.closest('a[href]') || a.querySelector('a[href]') || a;
    const words = t => /[\p{L}\p{N}]/u.test(t) ? t : '';
    let label = words(txt(link).replace(re, '').replace(/topic|page|[←→‹›«»<>]/gi, '').trim());
    if (!label) {  // "NEXT TOPIC →" with the target's title printed next to it; stop before the other pager
      for (let e = link.parentElement; e && e !== document.body && !label; e = e.parentElement) {
        if ([...e.querySelectorAll('a[href]')].some(x => x.href !== link.href)) break;
        label = words(txt(e).replace(txt(link), '').replace(re, '').replace(/topic|[←→‹›«»<>]/gi, '').trim());
      }
    }
    // the pager bar itself (its small wrapping container) is chrome, not body content: it must not be
    // sampled as a body paragraph/hyperlink, nor counted in the overlap/image scans below
    for (let e = link, bar = link; e && e !== root; e = e.parentElement) {
      bar = e;
      if (e.querySelectorAll('a[href]').length > 2) { pagerBars.push(bar); break; }
      if (e.parentElement === root) { pagerBars.push(bar); break; }
    }
    return { text: txt(link), label: label.slice(0, 200), href: link.href || '', where: path(link), visible: visible(link), box: box(link) };
  };
  const next = pager(sel.next, /\bnext\b/i), prev = pager(sel.prev, /\bprev(ious)?\b/i);
  panels.push(...pagerBars);
  // download PDF
  const dl = (sel.download ? qa(sel.download) : qa('a[href], button, [role="button"]').filter(e =>
      /download/i.test(txt(e) + ' ' + (e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('title') || '')) ||
      /\.pdf(\?|#|$)/i.test(e.getAttribute('href') || '') || e.hasAttribute('download')))
    .filter(e => !inPanels(e) || /pdf/i.test(txt(e)))
    .map(e => ({ text: txt(e) || e.getAttribute('aria-label') || '', href: e.href || e.getAttribute('data-href') || e.getAttribute('data-url') || '',
                 tag: e.tagName.toLowerCase(), where: path(e), visible: visible(e) }));
  // header: product title (subtitle)
  const header = q(sel.header) || qa('header, [role="banner"]').filter(visible)[0] || null;
  const headTexts = [];
  const scope = header ? [...header.querySelectorAll('*')] : qa('body *').filter(e => { const b = box(e); return b[3] < 160 && !root.contains(e); });
  for (const e of scope) {
    if (!visible(e) || e.closest('button, select, input, [role="button"], [role="search"], form, [role="listbox"]') || inPanels(e)) continue;
    const own = [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.data).join(' ').replace(/\s+/g, ' ').trim();
    if (!own || own.length > 120 || /download|search|english|language/i.test(own)) continue;
    const cs = getComputedStyle(e), b = box(e);
    const logo = !!(e.closest('a') && e.closest('a').querySelector('img, svg'));
    const hint = /product|subtitle|sub-title|guide-?title|doc-?title|book-?title|manual|series|title/i.test((e.className || '') + ' ' + (e.id || '') + ' ' + (e.parentElement ? e.parentElement.className : ''));
    headTexts.push({ text: own, where: path(e), size: parseFloat(cs.fontSize), weight: +cs.fontWeight || 400, box: b, logo, hint });
  }
  let sub = q(sel.subtitle);
  let subtitle = null;
  if (sub) subtitle = { text: txt(sub), where: path(sub), how: 'selector' };
  else if (headTexts.length) {
    const top = Math.min(...headTexts.map(h => h.box[1]));
    const score = h => (h.hint ? 3 : 0) + (h.weight >= 600 ? 1 : 0) + (h.logo ? -3 : 0) + (h.box[1] > top + 10 ? 1 : 0) + h.size / 100;
    const best = [...headTexts].sort((a, b) => score(b) - score(a))[0];
    // parts on the same row next to it ("Monitor arm BSH Series | Monitor")
    const row = headTexts.filter(h => h !== best && Math.abs(h.box[1] - best.box[1]) < 12 && h.box[0] > best.box[0] && h.box[0] - best.box[2] < 120 && !h.logo);
    subtitle = { text: best.text, where: best.where, how: 'header heuristic', also: row.map(h => h.text) };
  }
  const footer = q(sel.footer) || qa('footer, [role="contentinfo"]').filter(visible).pop() || null;
  const crumbs = q(sel.breadcrumb) || qa('[aria-label*="breadcrumb" i], .breadcrumb, .breadcrumbs, [class*="breadcrumb"]')[0] || null;
  if (crumbs) panels.push(crumbs);  // chrome, not content: never sampled as a body heading/paragraph/hyperlink
  const heads = [...root.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(h => visible(h) && txt(h) && !inPanels(h))
    .map(h => ({ level: +h.tagName[1], text: txt(h), id: h.id || (h.closest('[id]') && h.closest('[id]') !== root ? h.closest('[id]').id : '') ||
                 (h.querySelector('[id], a[name]') ? (h.querySelector('[id]')?.id || h.querySelector('a[name]').getAttribute('name')) : '') }));
  // the gap between the breadcrumb and the H1 below it: present and measured (null when either is missing),
  // so a collapsed/overlapping gap or a missing breadcrumb above a heading is caught, not just guessed at
  const h1El = root.querySelector('h1');
  let breadcrumbGap = null;
  if (crumbs && visible(crumbs) && h1El && visible(h1El)) {
    const cr = crumbs.getBoundingClientRect(), hr = h1El.getBoundingClientRect();
    breadcrumbGap = Math.round(hr.top - cr.bottom);
  }
  // ---- page layout and pictures: what is broken on the page itself (no prod needed)
  const rb = root.getBoundingClientRect();
  const R = [rb.left + scrollX, rb.right + scrollX];
  const layout = [];
  const doc = document.documentElement;
  if (doc.scrollWidth > doc.clientWidth + 4)
    layout.push({ kind: 'page-scroll', text: 'The page scrolls sideways', detail: `content ${doc.scrollWidth}px wide in a ${doc.clientWidth}px window`, where: '', box: box(root) });
  // the page is only its H1 (or other headings), nothing else below: a topic with no body at all
  if (h1El) {
    const body = [...root.querySelectorAll('p, li, td, th, pre, blockquote, img, svg, table, figure, dl, [role="note"]')]
      .filter(e => visible(e) && !inPanels(e) && (e.tagName === 'IMG' || e.tagName === 'SVG' || e.tagName === 'TABLE' || e.tagName === 'FIGURE' || txt(e).trim()));
    if (!body.length)
      layout.push({ kind: 'empty-page', text: txt(h1El), detail: 'Only the heading is on the page, no body content below it', where: path(h1El), box: box(h1El) });
  }
  const pics = deepQueryAll(root, 'img, svg, picture > img').filter(e => !inPanels(e) && !(e.tagName === 'svg' && e.closest('a,button')));
  const images = [];
  for (const im of pics) {
    const r = im.getBoundingClientRect();
    const isImg = im.tagName === 'IMG';
    const name = (isImg ? (im.getAttribute('alt') || (im.currentSrc || im.src || '').split('/').pop().split('?')[0]) : 'svg') || 'image';
    if (isImg && im.complete && !im.naturalWidth) { layout.push({ kind: 'image-broken', text: name, detail: 'the picture did not load', where: path(im), box: box(im) }); continue; }
    if (!visible(im) || r.width < 8 || r.height < 8) {
      if (isImg && im.naturalWidth > 8 && visible(im.parentElement)) layout.push({ kind: 'image-collapsed', text: name, detail: `drawn ${Math.round(r.width)}×${Math.round(r.height)}px (its file is ${im.naturalWidth}×${im.naturalHeight}px)`, where: path(im), box: box(im.parentElement) });
      continue;
    }
    const left = r.left + scrollX, right = r.right + scrollX;
    // the box the picture sits in: the nearest block ancestor
    let boxEl = im.parentElement;
    while (boxEl && boxEl !== root && getComputedStyle(boxEl).display.startsWith('inline')) boxEl = boxEl.parentElement;
    const pb = (boxEl || root).getBoundingClientRect();
    if (right > R[1] + 3 || left < R[0] - 3)
      layout.push({ kind: 'image-overflow', text: name, detail: `runs ${Math.round(Math.max(right - R[1], R[0] - left))}px outside the content area`, where: path(im), box: box(im) });
    else if (r.width > pb.width + 3)
      layout.push({ kind: 'image-overflow', text: name, detail: `${Math.round(r.width)}px wide in a ${Math.round(pb.width)}px box`, where: path(im), box: box(im) });
    if (isImg && im.naturalWidth && im.naturalHeight) {
      const drawn = r.width / r.height, own = im.naturalWidth / im.naturalHeight;
      const fit = getComputedStyle(im).objectFit;
      if (Math.abs(drawn / own - 1) > 0.06 && !['contain', 'cover', 'scale-down', 'none'].includes(fit))
        layout.push({ kind: 'image-stretched', text: name, detail: `drawn ${Math.round(r.width)}×${Math.round(r.height)}px, its file is ${im.naturalWidth}×${im.naturalHeight}px (proportions changed)`, where: path(im), box: box(im) });
      if (r.width > im.naturalWidth * 1.5 && r.width > 120 && !/\.svg(\?|$)/i.test(im.currentSrc || im.src || ''))
        layout.push({ kind: 'image-upscaled', text: name, detail: `drawn ${Math.round(r.width)}px wide from a ${im.naturalWidth}px file: it will look blurred`, where: path(im), box: box(im) });
    }
    // where the picture sits in its box: left, centre or right (a picture as wide as its box is "full")
    const free = pb.width - r.width, off = r.left - pb.left;
    const align = free < 0.08 * pb.width ? 'full' : off < 0.2 * free ? 'left' : off > 0.8 * free ? 'right' : Math.abs(off - free / 2) < 0.15 * free ? 'center' : 'other';
    images.push({ name, width: Math.round(r.width), height: Math.round(r.height), natural: isImg ? [im.naturalWidth, im.naturalHeight] : null,
                  share: Math.round(100 * r.width / Math.max(1, rb.width)), align, top: Math.round(r.top + scrollY), where: path(im), box: box(im) });
  }
  // blocks that run out of the content area: tables, code, wide boxes
  for (const e of root.querySelectorAll('table, pre, iframe, video, .table, [class*="col"], [class*="flex"], [class*="grid"]')) {
    if (inPanels(e) || !visible(e)) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 40) continue;
    const over = Math.max(r.right + scrollX - R[1], R[0] - (r.left + scrollX));
    const scrolls = /(auto|scroll)/.test(getComputedStyle(e.parentElement || e).overflowX);
    if (over > 4 && !scrolls)
      layout.push({ kind: 'block-overflow', text: (e.tagName.toLowerCase() + (e.className && typeof e.className === 'string' ? '.' + e.className.trim().split(/\s+/)[0] : '')), detail: `runs ${Math.round(over)}px outside the content area: ${txt(e).slice(0, 60)}`, where: path(e), box: box(e) });
  }
  // text run over by a picture, or two text blocks printed on top of each other
  const blocks = [...root.querySelectorAll('p, li, h1, h2, h3, h4, h5, h6, td, th, figcaption')].filter(e => visible(e) && !inPanels(e) && txt(e).length > 2 && !e.querySelector('p, li, table'));
  const rect = e => e.getBoundingClientRect();
  const cut = (a, b) => Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left)) * Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
  for (const im of pics) {
    const r = rect(im);
    if (r.width < 24 || r.height < 24 || !visible(im)) continue;
    for (const b of blocks) {
      if (b.contains(im) || im.contains(b)) continue;
      const t = document.createRange(); t.selectNodeContents(b);
      const tr = t.getBoundingClientRect();
      if (tr.width < 4 || tr.height < 4) continue;
      const a = cut(r, tr);
      if (a > 0.25 * Math.min(r.width * r.height, tr.width * tr.height) && a > 300) {
        layout.push({ kind: 'overlap', text: txt(b).slice(0, 60), detail: 'a picture is drawn over this text', where: path(b), box: box(b) });
        break;
      }
    }
  }
  for (let i = 0; i + 1 < blocks.length && layout.length < 200; i++) {
    const a = blocks[i], b = blocks[i + 1];
    if (a.contains(b) || b.contains(a)) continue;
    const ra = rect(a), rc = rect(b);
    const o = cut(ra, rc);
    if (o > 0.4 * Math.min(ra.width * ra.height, rc.width * rc.height) && o > 200)
      layout.push({ kind: 'overlap', text: txt(b).slice(0, 60), detail: `printed on top of “${txt(a).slice(0, 40)}”`, where: path(b), box: box(b) });
  }
  if (breadcrumbGap !== null && breadcrumbGap < 0)
    layout.push({ kind: 'breadcrumb-overlap', text: txt(crumbs).slice(0, 60),
                 detail: `the H1 overlaps the breadcrumb by ${-breadcrumbGap}px`, where: path(h1El), box: box(h1El) });
  // what each #fragment of this page lands on
  const targets = {};
  for (const it of items(otp)) {
    const f = decodeURIComponent((it.href.split('#')[1] || ''));
    if (!f || f in targets) continue;
    const t = document.getElementById(f) || document.getElementsByName(f)[0];
    targets[f] = t ? (t.matches('h1,h2,h3,h4,h5,h6') ? txt(t) : txt(t.querySelector('h1,h2,h3,h4,h5,h6')) || txt(t).slice(0, 120)) : null;
  }
  // ---- CSS vs the design spec (config/typography.toml [typography.formats.html_web]): one real sample
  // per role, so Python can compare it against the spec's font / weight / size / line-height / colour
  const toHex = c => { const m = (c || '').match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)/); return m ? '#' + [1, 2, 3].map(i => (+m[i]).toString(16).padStart(2, '0')).join('') : null; };
  const sample = el => { if (!el) return null; const cs = getComputedStyle(el);
    return { family: cs.fontFamily.split(',')[0].replace(/["']/g, '').trim(), weight: +cs.fontWeight || 400,
             size: Math.round(parseFloat(cs.fontSize) || 0),
             lineHeight: cs.lineHeight === 'normal' ? null : Math.round(parseFloat(cs.lineHeight) || 0),
             color: toHex(cs.color), underline: cs.textDecorationLine.includes('underline'),
             marginTop: Math.round(parseFloat(cs.marginTop) || 0), marginBottom: Math.round(parseFloat(cs.marginBottom) || 0),
             text: txt(el).slice(0, 60), where: path(el), box: box(el) }; };
  const firstOf = s => [...root.querySelectorAll(s)].find(e => visible(e) && !inPanels(e) && txt(e));
  const css = {
    h1: sample(firstOf('h1')), h2: sample(firstOf('h2')), h3: sample(firstOf('h3')),
    body_default: sample(firstOf('p')), body_strong: sample(firstOf('strong, b')),
    body_hyperlink: sample(firstOf('a[href]')), table_header: sample(firstOf('table th')),
    table_default: sample(firstOf('table td')),
    callout_title: sample([...root.querySelectorAll('*')].find(e => visible(e) && !inPanels(e) && e.children.length === 0 &&
      /^(important|note|tip|warning)$/i.test(txt(e).trim()) && (+getComputedStyle(e).fontWeight || 400) >= 600)),
  };
  // ---- the vertical gap between adjacent content blocks (paragraph / table / image / callout), vs
  // config/typography.toml [typography.formats.html_web].block_gaps - headings are not in this list,
  // see heading_spacing (margins) above instead
  const isCallout = e => /^(important|note|tip|warning)$/i.test(txt(e.firstElementChild || e).trim().slice(0, 20)) ||
    /callout|note|tip|warning|important/i.test(e.className || '');
  const blockKind = e => {
    if (/^H[1-6]$/.test(e.tagName)) return 'heading';
    if (e.tagName === 'TABLE') return 'table';
    if (e.tagName === 'IMG' || e.tagName === 'FIGURE' || e.tagName === 'SVG') return 'image';
    if (isCallout(e)) return 'callout';
    return 'paragraph';
  };
  const contentBlocks = [...root.querySelectorAll('p, table, figure, img, svg, div, h1, h2, h3, h4, h5, h6')].filter(e => {
    if (inPanels(e) || !visible(e)) return false;
    if (e.tagName === 'IMG' && e.closest('figure')) return false;  // counted through its own <figure>
    if (e.tagName === 'DIV') return isCallout(e) && !e.querySelector('p, table, figure, img, svg');
    return !!txt(e).trim() || e.tagName === 'TABLE' || e.tagName === 'IMG' || e.tagName === 'FIGURE';
  });
  const gaps = [];
  for (let i = 0; i + 1 < contentBlocks.length; i++) {
    const a = contentBlocks[i], b = contentBlocks[i + 1];
    if (a.contains(b) || b.contains(a)) continue;
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    const gap = Math.round(rb.top - ra.bottom);
    if (gap < -2 || gap > 200) continue;  // overlapping, or the two are not really adjacent (a page break)
    gaps.push({ from: blockKind(a), to: blockKind(b), gap, where: path(b) });
  }
  return {
    url: location.href, title: document.title, h1: txt(root.querySelector('h1')),
    nav: nav ? { label: navLabel, where: path(nav), items: items(nav) } : null,
    otp: otp ? { label: otpLabel, where: path(otp), items: items(otp), targets } : null,
    next, prev, download: dl, subtitle, header: headTexts.map(({ text, where, size, weight }) => ({ text, where, size, weight })),
    footer: footer ? txt(footer).slice(0, 400) : '', breadcrumb: crumbs ? txt(crumbs) : '', breadcrumb_gap: breadcrumbGap,
    breadcrumb_box: crumbs ? box(crumbs) : null, breadcrumb_items: crumbs ? items(crumbs) : [], headings: heads,
    layout: layout.slice(0, 200), images: images.slice(0, 400), content_width: Math.round(rb.width),
    ids: [...document.querySelectorAll('[id]')].map(e => e.id).slice(0, 5000), css, gaps: gaps.slice(0, 300),
  };
}
"""

SELECTOR_KEYS = ("nav", "on_this_page", "next", "prev", "download", "header", "subtitle", "footer", "breadcrumb")


def read_chrome(page, cfg: dict, root: str = "") -> dict | None:
    """The chrome of the loaded page (CHROME_JS); None when it cannot be read."""
    sel = {k: (cfg or {}).get(k, "") for k in SELECTOR_KEYS}
    try:
        return page.evaluate(CHROME_JS, [sel, root])
    except Exception as e:  # never fail the content run because of the chrome
        return {"url": page.url, "error": f"{type(e).__name__}: {e}"}


def key(u: str) -> str:
    """scheme://host/path: the page a link opens (query and #fragment aside)."""
    s = urlsplit(u or "")
    return f"{s.scheme}://{s.netloc}{s.path}"


def probe(page, pages: list[dict], visited: dict, out: Path, report=None) -> dict:
    """With the browser still open: open what the chrome points to.
    - every navigation / next / previous target that the crawl did not open (HTTP status, title)
    - the Download PDF file (saved as site-download.pdf)
    - the AEM property the header's product title is read from (author instance only)"""
    say = report or (lambda m: None)
    targets = {}
    for p in pages:
        for it in ((p.get("nav") or {}).get("items") or []):
            targets.setdefault(key(it["href"]), it["href"])
        for it in (p.get("breadcrumb_items") or []):
            if it.get("href"):
                targets.setdefault(key(it["href"]), it["href"])
        for pg in (p.get("next"), p.get("prev")):
            if pg and pg.get("href"):
                targets.setdefault(key(pg["href"]), pg["href"])
    status = dict(visited)
    todo = [(k, u) for k, u in targets.items() if k not in status and urlsplit(u).scheme in ("http", "https")]
    # the browser's own session (cookies, basic auth), read once here (its only safe use from another thread:
    # sync Playwright objects must stay on this one thread) and replayed as plain HTTP from here on, several
    # links at once - checking a link never has to drive the browser itself, one page at a time
    cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in page.context.cookies())

    def one(u: str) -> dict:
        import urllib.error
        from urllib.request import Request, urlopen
        try:
            req = Request(u, headers={"Cookie": cookie_header} if cookie_header else {})
            with urlopen(req, timeout=30) as resp:
                body = resp.read(1_000_000).decode("utf-8", "replace") if "html" in (resp.headers.get("content-type") or "") else ""
                status_code, final_url = resp.status, resp.geturl()
        except urllib.error.HTTPError as e:
            body, status_code, final_url = (e.read(1_000_000).decode("utf-8", "replace") if "html" in (e.headers.get("content-type") or "") else ""), e.code, e.url
        except Exception as e:
            return {"status": 0, "url": u, "error": f"{type(e).__name__}: {e}", "crawled": False}
        m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        h = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S | re.I)
        strip = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()
        ids = re.findall(r"""\s(?:id|name)=["']([^"']+)["']""", body)
        return {"status": status_code, "url": final_url, "title": strip(m.group(1)) if m else "",
               "h1": strip(h.group(1)) if h else "", "ids": ids[:5000], "crawled": False}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        n = 0
        for (k, u), result in zip(todo[:200], ex.map(lambda ku: one(ku[1]), todo[:200])):
            n += 1
            say(f"Checking navigation link {n}/{len(todo)}")
            status[k] = result
    return {"links": status, "download": _download(page, pages, out, say), "subtitle_source": _subtitle_source(page, pages)}


def _download(page, pages: list[dict], out: Path, say) -> dict:
    """Fetch the Download PDF file of the first page that has one."""
    first = next(((p, d) for p in pages for d in (p.get("download") or []) if d.get("visible")), None) or \
        next(((p, d) for p in pages for d in (p.get("download") or [])), None)
    if not first:
        return {"found": False}
    p, d = first
    res = {"found": True, "text": d["text"], "href": d["href"], "where": d["where"], "page": p["url"]}
    path = out / "site-download.pdf"
    say("Downloading the site's PDF")
    try:
        if d["href"] and urlsplit(d["href"]).scheme in ("http", "https"):
            r = page.request.get(d["href"], timeout=90_000, max_redirects=10)
            res.update(status=r.status, content_type=r.headers.get("content-type", ""), final_url=r.url)
            data = r.body()
        else:  # a button that starts the download from script
            if key(page.url) != key(p["url"]):
                page.goto(p["url"], wait_until="load", timeout=90_000)
            with page.expect_download(timeout=60_000) as info:
                page.locator(d["where"] or "body").first.click()
            dl = info.value
            dl.save_as(path)
            data = path.read_bytes()
            res.update(status=200, content_type="(browser download)", final_url=dl.url, filename=dl.suggested_filename)
        res["bytes"] = len(data)
        res["is_pdf"] = data[:5] == b"%PDF-"
        if res["is_pdf"]:
            path.write_bytes(data)
            res["file"] = path.name
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
    return res


def _subtitle_source(page, pages: list[dict]) -> dict:
    """AEM: the page (or a parent page) property whose value is the product title shown in the header.
    Reads <page>/jcr:content.json up the tree; works on author / where the Sling JSON is open."""
    sub = next((p["subtitle"]["text"] for p in pages if p.get("subtitle")), "")
    if not sub or not pages:
        return {}
    s = urlsplit(pages[0]["url"])
    if "/content/" not in s.path:
        return {}
    parts = re.sub(r"\.html$", "", s.path).split("/")
    want = normalize.title(sub)
    tried = []
    for n in range(len(parts), 2, -1):
        base = "/".join(parts[:n])
        url = f"{s.scheme}://{s.netloc}{base}/jcr:content.json"
        tried.append(base)
        try:
            r = page.request.get(url, timeout=15_000)
            if r.status != 200:
                continue
            props = r.json()
        except Exception:
            continue
        hits = [k for k, v in props.items() if isinstance(v, str) and normalize.title(v) == want]
        near = [k for k, v in props.items() if isinstance(v, str) and 3 < len(v) < 200 and want and want in normalize.title(v)]
        if hits or near:
            k = (hits or near)[0]
            return {"page": base, "property": k, "value": props[k], "exact": bool(hits)}
    return {"not_found_in": tried}


# ---------------------------------------------------------------- validation against the PDF

def _norm(s: str) -> str:
    return normalize.title(re.sub(r"^\s*\d+(\.\d+)*\.?\s+", "", s or ""))


def _same(a: str, b: str, ratio: float = 0.9) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a == b or SequenceMatcher(None, a, b).ratio() >= ratio)


def _row(group: str, status: str, item: str, expected: str = "", actual: str = "", page: str = "", note: str = "",
         where: str = "", shot: dict | None = None) -> dict:
    r = {"group": group, "status": status, "item": item, "expected": expected, "actual": actual,
         "page": page, "note": note, "where": where}
    if shot:  # {"page_shot": "site-shots/<hash>.png", "box": [x0, y0, x1, y1]} - crops a screenshot for the report
        r["shot"] = shot
    return r


GROUPS = [("nav", "Left navigation — whole TOC"), ("links", "Navigation links"), ("download", "Download PDF"),
          ("pager", "Next / previous topic"), ("otp", "On this page"), ("layout", "Page layout and pictures"),
          ("breadcrumb", "Breadcrumb"), ("subtitle", "Product subtitle"), ("typography", "CSS vs design spec (HTML Web)")]
LAYOUT_ISSUE = {"page-scroll": "Page scrolls sideways", "image-broken": "Picture not loaded", "image-collapsed": "Picture collapsed",
                "image-overflow": "Picture outside its area", "image-stretched": "Picture stretched", "image-upscaled": "Picture enlarged (blurred)",
                "block-overflow": "Content outside the page area", "overlap": "Content overlapping",
                "breadcrumb-overlap": "Breadcrumb / H1 overlap", "empty-page": "Page has only a heading, no content"}
# kinds always checked (on the active validation list: broken / missing / pixelated images, table/content
# breaking out of the page); "overlap" and "empty-page" are old checks, off by default - see [site]
# check_overlap / check_empty_page. "breadcrumb-overlap" follows check_breadcrumb, on by default.
CORE_LAYOUT_KINDS = {"page-scroll", "image-broken", "image-collapsed", "image-overflow", "image-stretched",
                      "image-upscaled", "block-overflow"}
_EXTRA_LAYOUT_TOGGLE = {"overlap": "check_overlap", "breadcrumb-overlap": "check_breadcrumb", "empty-page": "check_empty_page"}
_EXTRA_LAYOUT_DEFAULT = {"overlap": False, "breadcrumb-overlap": True, "empty-page": False}

_ROLE_LABEL = {"h1": "Headline 1", "h2": "Headline 2", "h3": "Headline 3", "body_default": "Body default",
               "body_strong": "Body strong", "body_hyperlink": "Body hyperlink", "table_header": "Table header",
               "table_default": "Table default", "callout_title": "Callout title", "p": "Paragraph"}


def _font_ok(family: str | None, want: str) -> bool:
    return bool(family) and family.lower().replace(" ", "").startswith(want.lower().replace(" ", ""))


def _css_rows(pages: list[dict], typography_cfg: dict) -> list[dict]:
    """Every role's live CSS (site_nav's `css` sample) vs config/typography.toml [typography.formats.html_web]:
    font family, weight (bold missing included), size, line height, colour, link underline - exact match,
    no tolerance (the spec is the contract: "follow this only exactly")."""
    t = typography_cfg or {}
    styles = ((t.get("formats") or {}).get("html_web") or {}).get("styles") or {}
    if not styles:
        return []
    fonts, weights = t.get("fonts") or {}, t.get("weights") or {}
    rows, seen = [], set()
    for p in pages:
        css = p.get("css") or {}
        for role, want in styles.items():
            got = css.get(role)
            if got is None or (role, p["url"]) in seen:
                continue
            seen.add((role, p["url"]))
            label = _ROLE_LABEL.get(role, role)
            want_family = fonts.get(want.get("font", ""), want.get("font", ""))
            want_weight = weights.get(want.get("weight", "Regular"), 400)
            bad = []
            shot = {"page_shot": p["page_shot"], "box": got["box"]} if p.get("page_shot") and got.get("box") else None
            if want_family and not _font_ok(got.get("family"), want_family):
                bad.append(("font family", want_family, got.get("family") or "—"))
            if got.get("weight") != want_weight:
                prop = "bold missing" if want_weight >= 600 > got.get("weight", 400) else "font weight"
                bad.append((prop, str(want_weight), str(got.get("weight"))))
            if want.get("size") is not None and got.get("size") != want["size"]:
                bad.append(("font size", f"{want['size']}px", f"{got.get('size')}px"))
            if want.get("line_height") is not None and got.get("lineHeight") is not None and got["lineHeight"] != want["line_height"]:
                bad.append(("line height", f"{want['line_height']}px", f"{got['lineHeight']}px"))
            want_color = (want.get("color") or "").lower()
            if want_color and want_color.startswith("#") and got.get("color") and got["color"].lower() != want_color:
                bad.append(("colour", want_color, got["color"]))
            if want.get("underline") and not got.get("underline"):
                bad.append(("underline", "underlined", "not underlined"))
            if bad:
                for prop, exp, act in bad:
                    rows.append(_row("typography", "fail", f"{label} — {prop}", exp, act, p["url"],
                                     got.get("text", ""), got.get("where", ""), shot=shot))
            else:
                rows.append(_row("typography", "pass", label, "", "matches the design spec", p["url"],
                                 where=got.get("where", ""), shot=shot))
    return rows


def _spacing_rows(pages: list[dict], typography_cfg: dict) -> list[dict]:
    """Space vs config/typography.toml [typography.formats.html_web]: each heading's own margin-top /
    margin-bottom (heading_spacing) and the vertical gap between adjacent content blocks - paragraph,
    table, image, callout (block_gaps) - exact match, no tolerance."""
    spec = ((typography_cfg or {}).get("formats") or {}).get("html_web") or {}
    heading_spacing, block_gaps = spec.get("heading_spacing") or {}, spec.get("block_gaps") or {}
    sizes, gap_rules = block_gaps.get("sizes") or {}, block_gaps.get("rules") or []
    rows, seen = [], set()
    for p in pages:
        css = p.get("css") or {}
        for role, want in heading_spacing.items():
            got = css.get(role if role != "p" else "body_default")
            if got is None or (role, p["url"]) in seen:
                continue
            seen.add((role, p["url"]))
            label = _ROLE_LABEL.get(role, role.upper())
            shot = {"page_shot": p["page_shot"], "box": got["box"]} if p.get("page_shot") and got.get("box") else None
            bad = []
            if want.get("margin_top") is not None and got.get("marginTop") != want["margin_top"]:
                bad.append(("space above", f"{want['margin_top']}px", f"{got.get('marginTop')}px"))
            if want.get("margin_bottom") is not None and got.get("marginBottom") != want["margin_bottom"]:
                bad.append(("space below", f"{want['margin_bottom']}px", f"{got.get('marginBottom')}px"))
            for prop, exp, act in bad:
                rows.append(_row("typography", "fail", f"{label} — {prop}", exp, act, p["url"],
                                 got.get("text", ""), got.get("where", ""), shot=shot))
            if not bad:
                rows.append(_row("typography", "pass", f"{label} spacing", "", "matches the design spec", p["url"],
                                 where=got.get("where", ""), shot=shot))
        for g in p.get("gaps") or []:
            rule = next((r for r in gap_rules if r.get("from") == g["from"] and r.get("to") == g["to"]), None)
            if not rule:
                continue
            want_gap = sizes.get(rule.get("size"))
            if want_gap is None:
                continue
            item = f"Space: {g['from']} → {g['to']}"
            if g["gap"] != want_gap:
                rows.append(_row("typography", "fail", item, f"{want_gap}px", f"{g['gap']}px", p["url"],
                                 where=g.get("where", "")))
            else:
                rows.append(_row("typography", "pass", item, "", "matches the design spec", p["url"], where=g.get("where", "")))
    return rows


def evaluate(site: dict, baseline: str, cfg: dict | None = None, typography_cfg: dict | None = None) -> dict:
    """Validate the captured chrome against the prod PDF. Returns {summary, groups, rows, pages}.
    `typography_cfg` is the run's [typography] config (config/typography.toml), used to check the live
    page's CSS against [typography.formats.html_web]."""
    import pymupdf

    cfg = cfg or {}
    pages = [p for p in site.get("pages") or [] if not p.get("error")]
    links = site.get("links") or {}
    rows: list[dict] = []
    # a page's own screenshot (html_source.py) + one page element's box -> a crop for the report (site.pdf);
    # None when either is missing (an issue about something that was never captured gets no crop, not a crash)
    shot_of = lambda p, it: {"page_shot": p["page_shot"], "box": it["box"]} if p.get("page_shot") and (it or {}).get("box") else None
    prod = pymupdf.open(baseline)
    l1 = [t for lvl, t, _ in prod.get_toc() if lvl == 1]
    l1_source = "PDF bookmarks (level 1)"
    if not l1 and site.get("toc_l1"):
        l1, l1_source = site["toc_l1"], "printed TOC (level 1)"

    # ---- left navigation: the whole TOC, every level - not only L1 - matched entry by entry the same way
    # the PDF's own TOC is matched against stage (pdfval.toc): same title -> same entry, a title repeated at
    # another level (a sub-item listed elsewhere, then the real heading) resolved towards the matching level
    navs = [p for p in pages if p.get("nav") and p["nav"]["items"]]
    if not pages:
        rows.append(_row("nav", "fail", "Chrome", note="No page could be read"))
    elif not navs:
        rows.append(_row("nav", "fail", "Left navigation", "a table of contents on every page", "not found",
                         note="Set [site] nav = \"<css selector>\" if the site has one"))
    else:
        from collections import Counter
        from . import toc as tocmod

        ref = navs[0]
        items = ref["nav"]["items"]
        toc_full = site.get("toc_full") or [(t, 1) for t in l1]
        # a shallow nav (top-level chapters only, sub-headings left to "On this page") is complete on its own
        # terms: only the TOC levels the nav itself actually goes to are compared, so it is not faulted for a
        # level it was never meant to carry
        nav_depth = max((it["depth"] for it in items), default=0) + 1
        toc_full = [(t, lvl) for t, lvl in toc_full if lvl <= nav_depth]
        thr = (cfg.get("toc") or {}).get("title_match_threshold", 0.8)
        entry = lambda title, level: tocmod.TocEntry(title, _norm(title), max(level, 1), None, -1, (0, 0, 0, 0), 0.0)
        toc_side = [entry(t, lvl) for t, lvl in toc_full]
        nav_side = [entry(it["text"], it["depth"] + 1) for it in items]
        aligned = tocmod._sequence(tocmod._align(toc_side, nav_side, thr), toc_side, nav_side, thr)
        counts = Counter()
        label = lambda e: f"L{e.level} “{e.title}”"
        for i, j, s, moved in aligned:
            ea, eb = (toc_side[i] if i is not None else None), (nav_side[j] if j is not None else None)
            if ea and eb and moved:
                status = "order differs"
            elif ea and eb:
                status = "match" if s == 1.0 and ea.level == eb.level else "level differs" if s == 1.0 else "title differs"
            else:
                status = "missing in stage" if ea else "extra in stage"
            counts[status] += 1
            sev = {"match": "pass", "order differs": "fail", "level differs": "warn", "title differs": "warn",
                  "missing in stage": "fail", "extra in stage": "info"}[status]
            nav_item = items[j] if j is not None and j < len(items) else None
            rows.append(_row("nav", sev, (ea or eb).title, label(ea) if ea else "—", label(eb) if eb else "—",
                             ref["url"], status if status != "match" else "", ref["nav"]["where"], shot=shot_of(ref, nav_item)))
        note = (f"{counts['match']} match · {counts['level differs']} level differs · {counts['title differs']} title differs · "
               f"{counts['order differs']} order differs · {counts['missing in stage']} missing in the navigation · "
               f"{counts['extra in stage']} in the navigation but not in the TOC")
        bad = counts["missing in stage"] or counts["order differs"]
        rows.insert(0, _row("nav", "fail" if bad else "warn" if counts["level differs"] or counts["title differs"] or counts["extra in stage"] else "pass",
                            "TOC entries compared", str(len(toc_side)), str(len(nav_side)), ref["url"], note, ref["nav"]["where"]))
        sig = lambda p: [(it["text"], key(it["href"])) for it in p["nav"]["items"]]
        for p in navs[1:]:
            if sig(p) != sig(ref):
                sa, sb = {x[0] for x in sig(ref)}, {x[0] for x in sig(p)}
                diff = ", ".join(f"-{x}" for x in sorted(sa - sb)) + " " + ", ".join(f"+{x}" for x in sorted(sb - sa))
                rows.append(_row("nav", "warn", "Same navigation on every page", f"{len(sig(ref))} entries as on the first page",
                                 f"{len(sig(p))} entries {diff.strip()}", p["url"], "Navigation differs from page to page"))
        for p in pages:
            if not p.get("nav"):
                rows.append(_row("nav", "fail", "Left navigation", "present", "not found", p["url"]))

    # ---- navigation links: open, land on the right page, active entry
    seen = set()
    for p in navs:
        for it in p["nav"]["items"]:
            k = key(it["href"])
            if (k, it["text"]) in seen:
                continue
            seen.add((k, it["text"]))
            rows.append(_link_row("links", it["text"], it["href"], links, p["url"],
                                  shot={"page_shot": p["page_shot"], "box": it["box"]} if p.get("page_shot") and it.get("box") else None))
    for p in navs:
        act = [it for it in p["nav"]["items"] if it["active"]]
        own = [it for it in p["nav"]["items"] if key(it["href"]) == key(p["url"]) and "#" not in (it["raw"] or "")]
        if not own:
            rows.append(_row("links", "warn", "Page in the navigation", _title(p), "not listed", p["url"],
                             "The page is not an entry of the left navigation"))
        elif not act:
            rows.append(_row("links", "warn", "Highlighted entry", own[0]["text"], "nothing highlighted", p["url"],
                             "The current page is not marked in the navigation"))
        elif not any(key(it["href"]) == key(p["url"]) for it in act):
            rows.append(_row("links", "fail", "Highlighted entry", own[0]["text"], ", ".join(a["text"] for a in act), p["url"],
                             "The navigation highlights another page"))
        else:
            rows.append(_row("links", "pass", "Highlighted entry", own[0]["text"], act[0]["text"], p["url"]))

    # ---- download PDF (old check, not on the active list - off by default, [site] check_download = true to restore)
    dl = site.get("download") or {}
    if cfg.get("check_download", False):
      if not dl.get("found"):
        rows.append(_row("download", "fail", "Download PDF button", "present", "not found", pages[0]["url"] if pages else "",
                         "Set [site] download = \"<css selector>\" if the site has one"))
      else:
        rows.append(_row("download", "pass", "Download PDF button", "present", dl["text"], dl["page"], where=dl["where"]))
        missing = [p["url"] for p in pages if not p.get("download")]
        for u in missing:
            rows.append(_row("download", "fail", "Download PDF button", "present", "not found", u))
        hrefs = {d["href"] for p in pages for d in (p.get("download") or []) if d["href"]}
        if len(hrefs) > 1:
            rows.append(_row("download", "warn", "Same file on every page", "one file", f"{len(hrefs)} different links",
                             note=" · ".join(sorted(hrefs))[:500]))
        if dl.get("error"):
            rows.append(_row("download", "fail", "Download", "a PDF file", dl["error"], dl["page"], dl.get("href", "")))
        elif dl.get("status", 0) >= 400:
            rows.append(_row("download", "fail", "Download", "HTTP 200", f"HTTP {dl['status']}", dl["page"], dl.get("href", "")))
        elif not dl.get("is_pdf"):
            rows.append(_row("download", "fail", "Download", "a PDF file",
                             f"{dl.get('content_type') or 'unknown type'}, {dl.get('bytes', 0)} bytes", dl["page"], dl.get("href", "")))
        else:
            rows.append(_row("download", "pass", "Download", "a PDF file", f"PDF, {dl['bytes'] // 1024} KB", dl["page"],
                             dl.get("final_url", "")))
            rows += _same_document(prod, Path(site["dir"]) / dl["file"], dl["page"])

    # ---- next / previous: old check, not on the active list - off by default, [site] check_pager = true to restore
    if cfg.get("check_pager", False):
      order = []
      for it in (navs[0]["nav"]["items"] if navs else []):
        k = key(it["href"])
        if "#" not in (it["raw"] or "") and k not in order:
            order.append(k)
      titles = {key(p["url"]): _title(p) for p in pages}
      for p in pages:
        k = key(p["url"])
        i = order.index(k) if k in order else None
        for name, pg, step in (("Next topic", p.get("next"), 1), ("Previous topic", p.get("prev"), -1)):
            exp = order[i + step] if i is not None and 0 <= i + step < len(order) else None
            exp_title = (titles.get(exp) or _nav_text(navs, exp)) if exp else ""
            if i is None:
                if pg:
                    rows.append(_link_row("pager", f"{name}: {pg['label'] or pg['text']}", pg["href"], links, p["url"],
                                          shot={"page_shot": p["page_shot"], "box": pg["box"]} if p.get("page_shot") and pg.get("box") else None))
                continue
            if not exp:
                rows.append(_row("pager", "fail" if pg and pg.get("visible") else "pass", name,
                                 "none (first page)" if step < 0 else "none (last page)",
                                 f"{pg['label'] or pg['text']} → {pg['href']}" if pg and pg.get("visible") else "none", p["url"],
                                 "" if not (pg and pg.get("visible")) else "The first/last page should not link further"))
                continue
            if not pg or not pg.get("href"):
                rows.append(_row("pager", "fail", name, exp_title, "missing", p["url"], "No link to the next page of the navigation"))
                continue
            got = key(pg["href"])
            if got != exp:
                rows.append(_row("pager", "fail", name, f"{exp_title} ({_short(exp)})", f"{pg['label'] or '—'} ({_short(got)})",
                                 p["url"], "Goes to another page than the navigation order", pg["where"]))
            elif pg["label"] and exp_title and not _same(pg["label"], exp_title, 0.85):
                rows.append(_row("pager", "warn", name, exp_title, pg["label"], p["url"],
                                 "Right page, but the label names another one", pg["where"]))
            else:
                st = links.get(got, {}).get("status", 200)
                rows.append(_row("pager", "pass" if 0 < st < 400 else "fail", name, exp_title, pg["label"] or pg["text"], p["url"],
                                 "" if 0 < st < 400 else f"HTTP {st}", pg["where"]))

    # ---- on this page: shown only when the page has H3/H4 sub-sections, and each entry must jump to the
    # right one - on the active list (H1 = page title, H2 = the page's own section: neither needs this list)
    if cfg.get("check_otp", True):
      for p in pages:
        heads = [h for h in p.get("headings") or [] if h["level"] >= 3]
        if not p.get("otp") or not p["otp"]["items"]:
            if len(heads) >= 1:
                rows.append(_row("otp", "fail", "On this page", f"{len(heads)} sub-heading(s) (h3/h4)", "not found", p["url"],
                                 "The page has h3/h4 sub-headings but no “On this page” list"))
            continue
        if not heads:
            rows.append(_row("otp", "warn", "On this page", "hidden (no h3/h4 on the page)", "shown", p["url"],
                             "No sub-heading to list: the “On this page” panel should be hidden", p["otp"]["where"]))
            continue
        its = [it for it in p["otp"]["items"] if "#" in it["href"]]
        top = min((h["level"] for h in heads), default=3)
        # every sub-heading down to H4 belongs in the list ([site] on_this_page_max_level); deeper ones only as
        # far as the list itself goes
        deep = max(int((cfg or {}).get("on_this_page_max_level", 4)), top + max(it["depth"] for it in its or [{"depth": 0}]))
        want = [h for h in heads if h["level"] <= deep]
        used = set()
        for h in want:
            k = next((i for i, it in enumerate(its) if i not in used and _same(h["text"], it["text"], 0.85)), None)
            if k is None:
                rows.append(_row("otp", "fail", h["text"], h["text"], "missing", p["url"], f"h{h['level']} not listed", p["otp"]["where"]))
                continue
            used.add(k)
            it = its[k]
            frag = unquote(it["href"].split("#", 1)[1])
            landed = (p["otp"].get("targets") or {}).get(frag)
            if landed is None:
                rows.append(_row("otp", "fail", it["text"], f"#{frag} on the page", "anchor not found", p["url"],
                                 "Clicking it does not jump to the section", p["otp"]["where"], shot=shot_of(p, it)))
            elif landed and not _same(landed, h["text"], 0.85) and not _same(landed, it["text"], 0.85):
                rows.append(_row("otp", "fail", it["text"], h["text"], f"jumps to “{landed[:80]}”", p["url"],
                                 "Clicking it jumps to the wrong section", p["otp"]["where"], shot=shot_of(p, it)))
            else:
                rows.append(_row("otp", "pass", it["text"], h["text"], it["text"], p["url"], where=p["otp"]["where"], shot=shot_of(p, it)))
        for i, it in enumerate(its):
            if i not in used:
                rows.append(_row("otp", "warn", it["text"], "", it["text"], p["url"], "Entry without a matching heading on the page",
                                 p["otp"]["where"]))

    # ---- page layout and pictures: broken / missing / pixelated images, table/content breaking out of the
    # page (and scrollable where it should be) - on the active list, always checked. "empty page" is an old
    # check, off by default: [site] check_empty_page = true to restore. Breadcrumb has its own group, below.
    for p in pages:
        issues = p.get("layout") or []
        for it in issues:
            kind = it["kind"]
            if kind not in CORE_LAYOUT_KINDS and not cfg.get(_EXTRA_LAYOUT_TOGGLE.get(kind, ""), _EXTRA_LAYOUT_DEFAULT.get(kind, False)):
                continue
            rows.append(_row("layout", "fail", LAYOUT_ISSUE.get(kind, kind), "", it.get("text", ""), p["url"],
                             it.get("detail", ""), it.get("where", ""), shot=shot_of(p, it)))
        if cfg.get("check_picture_alignment", True):
            pics = [im for im in p.get("images") or [] if im["width"] >= 60]  # icons aside
            sides = {im["align"] for im in pics} & {"left", "center", "right"}
            if len(sides) > 1 and len(pics) >= 2:
                by = {a: [im for im in pics if im["align"] == a] for a in sides}
                usual = max(by, key=lambda a: len(by[a]))
                for a, ims in by.items():
                    if a != usual and len(ims) <= max(1, len(by[usual]) // 2):  # the odd ones out
                        for im in ims:
                            rows.append(_row("layout", "warn", "Picture alignment", f"{usual} (as the page's other pictures)", f"{im['name']}: {a}",
                                             p["url"], f"{im['width']}×{im['height']}px, {im['share']}% of the content width", im["where"],
                                             shot=shot_of(p, im)))
        if "layout" in p and not [i for i in issues if i["kind"] in CORE_LAYOUT_KINDS]:
            rows.append(_row("layout", "pass", "Page layout", "", f"{len(p.get('images') or [])} picture(s) checked", p["url"]))

    # ---- breadcrumb: present above the H1, each crumb's link opens (no broken link) and lands on the
    # right page (no wrong redirect), and the gap above the H1 matches the spec (config/typography.toml
    # [typography.formats.html_web].heading_spacing.h1.margin_top: the H1 sits right under the breadcrumb,
    # with no extra/missing air) - on the active list by default ([site] check_breadcrumb = false to drop)
    if cfg.get("check_breadcrumb", True):
        want_gap = (((typography_cfg or {}).get("formats") or {}).get("html_web") or {}).get(
            "heading_spacing", {}).get("h1", {}).get("margin_top")
        for p in pages:
            h1, gap = p.get("h1") or "", p.get("breadcrumb_gap")
            if h1 and not p.get("breadcrumb"):
                rows.append(_row("breadcrumb", "warn", "Breadcrumb above H1", "a breadcrumb trail", "not found", p["url"],
                                 "The page has an H1 but no breadcrumb above it"))
                continue
            if h1 and p.get("breadcrumb") and gap is not None and gap >= 0:
                if want_gap is not None and gap != want_gap:
                    rows.append(_row("breadcrumb", "fail", "Space above H1 (breadcrumb)", f"{want_gap}px",
                                     f"{gap}px", p["url"], "The gap between the breadcrumb and the H1 does not match the design spec"))
                else:
                    rows.append(_row("breadcrumb", "pass", "Space above H1 (breadcrumb)", "", f"{gap}px gap to the breadcrumb", p["url"]))
            for it in p.get("breadcrumb_items") or []:
                if not it.get("href"):
                    continue
                rows.append(_link_row("breadcrumb", it["text"], it["href"], links, p["url"], shot=shot_of(p, it), check_title=False))



    # ---- CSS vs the design spec (config/typography.toml [typography.formats.html_web]): on the active list
    if cfg.get("check_typography", True):
        rows += _css_rows(pages, typography_cfg or {})
        rows += _spacing_rows(pages, typography_cfg or {})

    # ---- product subtitle: old check, not on the active list - off by default, [site] check_subtitle = true to restore
    if cfg.get("check_subtitle", False):
        rows += _subtitle_rows(pages, prod, site.get("subtitle_source") or {}, baseline)
    prod.close()

    counts = {g: {"pass": 0, "warn": 0, "fail": 0, "info": 0} for g, _ in GROUPS}
    for r in rows:
        counts[r["group"]][r["status"]] += 1
    summary = {"pages": len(pages), "fail": sum(c["fail"] for c in counts.values()),
               "warn": sum(c["warn"] for c in counts.values()), "pass": sum(c["pass"] for c in counts.values()),
               "groups": counts, "status": "fail" if any(c["fail"] for c in counts.values()) else
               "warn" if any(c["warn"] for c in counts.values()) else "pass", "l1": l1}
    page_rows = [{"url": p["url"], "title": _title(p), "nav": (p.get("nav") or {}).get("where", ""),
                  "on_this_page": (p.get("otp") or {}).get("where", ""), "subtitle": (p.get("subtitle") or {}).get("text", ""),
                  "next": (p.get("next") or {}).get("href", ""), "prev": (p.get("prev") or {}).get("href", ""),
                  "footer": p.get("footer", ""), "breadcrumb": p.get("breadcrumb", "")} for p in pages]
    return {"summary": summary, "groups": [{"id": g, "title": t} for g, t in GROUPS], "rows": rows, "pages": page_rows,
            "download": {k: v for k, v in dl.items() if k != "error"} | ({"error": dl["error"]} if dl.get("error") else {}),
            "subtitle_source": site.get("subtitle_source") or {}}


def _title(p: dict) -> str:
    return p.get("h1") or re.split(r"\s+[|–-]\s+", p.get("title") or "")[0]


def _nav_text(navs: list, k: str | None) -> str:
    for p in navs[:1]:
        for it in p["nav"]["items"]:
            if key(it["href"]) == k:
                return it["text"]
    return ""


def _short(u: str) -> str:
    return urlsplit(u or "").path.rsplit("/", 1)[-1] or u


def _link_row(group: str, text: str, href: str, links: dict, page: str, shot: dict | None = None,
              check_title: bool = True) -> dict:
    k = key(href)
    st = links.get(k)
    frag = href.split("#", 1)[1] if "#" in href else ""
    if urlsplit(href).scheme not in ("http", "https"):
        return _row(group, "warn", text, "a page link", href, page, "Not an http(s) link", shot=shot)
    if not st:
        return _row(group, "warn", text, "opens", "not checked", page, href, shot=shot)
    if st.get("error") or not 0 < st.get("status", 0) < 400:
        return _row(group, "fail", text, "HTTP 200", st.get("error") or f"HTTP {st['status']}", page, href, shot=shot)
    landed = st.get("h1") or re.split(r"\s+[|–-]\s+", st.get("title") or "")[0]
    if frag and frag not in (st.get("ids") or []):
        return _row(group, "fail", text, f"#{frag} on {_short(href)}", "anchor not found", page, href, shot=shot)
    # a left-nav / pager link's own text is the target page's title: it must match. A breadcrumb crumb's
    # label ("Home", a category name) is not: check_title=False there - only that the link opens, not what it says
    if check_title and landed and not _same(landed, text, 0.8) and not _same(st.get("title", ""), text, 0.8) and not frag:
        return _row(group, "fail", text, text, f"opens “{landed}”", page, f"{href} → {st.get('url', '')}", shot=shot)
    return _row(group, "pass", text, text, landed or f"HTTP {st['status']}", page, href, shot=shot)


def _words(doc, limit: int = 0) -> set[str]:
    out = set()
    for k, pg in enumerate(doc):
        if limit and k >= limit:
            break
        out.update(w for w in re.findall(r"\w+", pg.get_text().lower()) if len(w) > 2)
    return out


def _same_document(prod, path: Path, page: str) -> list[dict]:
    import pymupdf
    try:
        d = pymupdf.open(path)
    except Exception as e:
        return [_row("download", "fail", "Opens as a PDF", "readable", f"{type(e).__name__}: {e}", page)]
    rows = []
    a, b = _words(prod), _words(d)
    sim = len(a & b) / max(len(a | b), 1)
    rows.append(_row("download", "pass" if sim >= 0.8 else "warn" if sim >= 0.5 else "fail", "Same document as prod",
                     f"prod: {len(prod)} pages, “{prod.metadata.get('title') or '—'}”",
                     f"download: {len(d)} pages, “{d.metadata.get('title') or '—'}”", page,
                     f"{sim:.0%} of the vocabulary shared" + ("" if sim >= 0.5 else " — a different manual?"), path.name))
    cov_a, cov_b = _cover(prod), _cover(d)
    if cov_a and cov_b:
        s = SequenceMatcher(None, normalize.title(cov_a), normalize.title(cov_b)).ratio()
        rows.append(_row("download", "pass" if s >= 0.8 else "warn", "Cover", cov_a, cov_b, page,
                         "" if s >= 0.8 else "The cover texts differ"))
    d.close()
    return rows


def _cover(doc) -> str:
    """The big text of the first page: the product and the kind of manual."""
    if not len(doc):
        return ""
    spans = [s for b in doc[0].get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"] if s["text"].strip()]
    if not spans:
        return ""
    big = [s["text"].strip() for s in spans if s["size"] >= 12] or [s["text"].strip() for s in spans]  # not slug / print marks
    return " · ".join(dict.fromkeys(big))[:300]


_VERSION = re.compile(r"\bv(?:ersion)?\s*\.?\s*(\d+(?:\.\d+)+)", re.I)


def _subtitle_rows(pages: list[dict], prod, source: dict, baseline: str) -> list[dict]:
    rows = []
    subs = [(p["url"], p["subtitle"]) for p in pages if p.get("subtitle")]
    if not subs:
        return [_row("subtitle", "fail", "Product subtitle", "a product title in the header", "not found",
                     pages[0]["url"] if pages else "", "Set [site] subtitle = \"<css selector>\" if the site has one")]
    url, s = subs[0]
    shown = s["text"] + (" | " + " | ".join(s["also"]) if s.get("also") else "")
    rows.append(_row("subtitle", "info", "Read from", "", shown, url, f"{s['how']}", s["where"]))
    if source.get("property"):
        rows.append(_row("subtitle", "pass" if source.get("exact") else "warn", "AEM source", "",
                         f"{source['property']} = “{source['value']}”", url, f"page property of {source['page']}"))
    elif source.get("not_found_in"):
        rows.append(_row("subtitle", "info", "AEM source", "", "not found", url,
                         "No jcr:content property of the page or its parents has this value (or the JSON is not readable)"))
    values: dict[str, list] = {}
    for u, x in subs:
        values.setdefault(x["text"], []).append((u, x))
    common = max(values, key=lambda v: len(values[v]))
    if len(values) > 1:
        for v, us in values.items():
            if v != common:
                for u, x in us:
                    rows.append(_row("subtitle", "fail", "Same on every page", common, v, u,
                                     f"{len(values[common])} page(s) show “{common}”", x["where"]))
    else:
        rows.append(_row("subtitle", "pass", "Same on every page", common, common, url, f"{len(subs)} page(s)"))
    for p in pages:
        if not p.get("subtitle"):
            rows.append(_row("subtitle", "fail", "Product subtitle", common, "not found", p["url"]))
    # does it name the product of the prod PDF?
    cover = _cover(prod)
    ref = " ".join([cover, prod.metadata.get("title") or "", prod.metadata.get("subject") or "", Path(baseline).stem])
    ref_tokens = {t.lower() for t in _tokens(ref)}
    for v, us in values.items():
        tokens = _tokens(v)
        models = [t for t in tokens if re.search(r"\d", t) or t.isupper() and len(t) > 1]
        key_tokens = models or [t for t in tokens if len(t) > 2]
        hit = [t for t in key_tokens if t.lower() in ref_tokens]
        ok = bool(key_tokens) and len(hit) >= max(1, len(key_tokens) // 2)
        rows.append(_row("subtitle", "pass" if ok else "fail", "Matches the prod PDF", cover or Path(baseline).stem, v, us[0][0],
                         (f"names {', '.join(hit)}" if ok else
                          f"None of {', '.join(key_tokens) or 'its words'} is on the PDF cover, title or file name — wrong product?")
                         + (f" · on {len(us)} page(s)" if len(values) > 1 else ""), us[0][1]["where"]))
    for extra in s.get("also") or []:
        e_tokens = [t for t in _tokens(extra) if len(t) > 2]
        e_hit = [t for t in e_tokens if t.lower() in ref_tokens]
        rows.append(_row("subtitle", "pass" if e_hit else "warn", "Category", cover, extra, url,
                         "" if e_hit else "Not on the PDF cover"))
    # version in the footer / title vs the PDF
    fv = next((m.group(1) for p in pages for m in [_VERSION.search((p.get("footer") or "") + " " + (p.get("subtitle") or {}).get("text", ""))] if m), "")
    pv = next((m.group(1) for m in [_VERSION.search(" ".join([Path(baseline).stem.replace("_", " "), cover,
                                                           " ".join(prod[i].get_text() for i in range(min(3, len(prod))))]))] if m), "")
    if fv or pv:
        rows.append(_row("subtitle", "pass" if fv and fv == pv else "warn", "Version", pv or "—", fv or "—", url,
                         "" if fv == pv else "The site's version (footer) and the PDF's version differ"))
    return rows


def _tokens(s: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+", s or "")


def write_csv(site: dict, path: Path) -> Path:
    titles = dict((g, t) for g, t in GROUPS)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Bug ID", "Check", "Status", "Item", "Expected", "Actual", "Page", "Note", "Element"])
        for r in site["rows"]:
            w.writerow([r.get("bug", ""), titles.get(r["group"], r["group"]), r["status"], r["item"], r["expected"], r["actual"],
                        r["page"], r["note"], r["where"]])
    return path
