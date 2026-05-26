#!/usr/bin/env python3
"""
gen_diagram.py — Dynamically generate visual images from blog post content.

Scans posts/*.md, detects diagram patterns (layer stacks, architecture blocks,
tables), and renders them as real SVG-based visual images via Playwright.

ABOUT "REAL IMAGES":
  - This script feeds proper SVG (shapes, gradients, colored boxes) to Playwright,
    so the output is a genuine visual diagram — not a screenshot of ASCII text.
  - For PHOTO-REALISTIC images (datacenter photos, server rack renders, etc.)
    an LLM image generation model IS required (see --ai-covers below).
  - For programmatic gradient covers: Pillow handles it — no LLM needed.

Usage:
    python gen_diagram.py                     # SVG diagrams from all posts
    python gen_diagram.py --post ai-clusters  # only posts whose slug matches
    python gen_diagram.py --covers            # also generate Pillow covers
    python gen_diagram.py --ai-covers         # DALL-E 3 covers (needs OPENAI_API_KEY)

Output:
    assets/images/diagrams/{slug}-stack.png      <- SVG layer stack diagram
    assets/images/diagrams/{slug}-arch-N.png     <- styled architecture terminal card
    assets/images/diagrams/{slug}-table-N.png    <- styled table image
    assets/images/blog/covers/{slug}.jpg         <- cover images
"""

import argparse
import os
import re
import sys
import textwrap
from pathlib import Path

WORKSPACE  = Path(__file__).parent
POSTS_DIR  = WORKSPACE / "posts"
ASSETS_DIR = WORKSPACE / "assets"
DIAG_DIR   = ASSETS_DIR / "images/diagrams"
COVER_DIR  = ASSETS_DIR / "images/blog/covers"
DIAG_DIR.mkdir(parents=True, exist_ok=True)


# ── Frontmatter parser ─────────────────────────────────────────────────────────

def parse_frontmatter(text):
    if not text.startswith("---"):
        return {}
    try:
        end = text.index("---", 3)
    except ValueError:
        return {}
    result = {}
    for line in text[3:end].split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result


def post_body(text):
    if not text.startswith("---"):
        return text
    try:
        idx = text.index("---", 3) + 3
        return text[idx:].strip()
    except ValueError:
        return text


# ── Pattern detection ──────────────────────────────────────────────────────────

BOX_CHARS = set("\u250c\u2510\u2514\u2518\u2502\u251c\u2524\u252c\u2534\u253c\u2500")


def has_box_chars(s):
    return any(c in BOX_CHARS for c in s)


def find_code_blocks(body):
    pattern = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)
    return [{"lang": m.group(1).strip(), "content": m.group(2)}
            for m in pattern.finditer(body)]


def find_md_tables(body):
    results = []
    lines = body.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                j += 1
            table = "\n".join(lines[i:j])
            if "---" in table:
                results.append(table)
            i = j
        else:
            i += 1
    return results


# ── ASCII layer stack parser ───────────────────────────────────────────────────

def parse_layer_stack(ascii_text):
    """
    Parse a numbered box-drawing layer stack diagram into
    [{'num', 'title', 'components'}, ...].
    Returns None if fewer than 3 layers are found.
    """
    BORDER_STARTS = {"\u250c", "\u251c", "\u2514"}  # ┌ ├ └
    VERT = "\u2502"                                  # │

    layers = []
    buf = []

    for line in ascii_text.split("\n"):
        s = line.rstrip()
        if not s:
            continue
        stripped = s.lstrip()
        if stripped and stripped[0] in BORDER_STARTS:
            if buf:
                m = re.match(r"^(\d+)\.\s+(.+)$", buf[0])
                if m:
                    layers.append({
                        "num":        int(m.group(1)),
                        "title":      m.group(2).strip(),
                        "components": "  \u00b7  ".join(buf[1:]) if len(buf) > 1 else "",
                    })
            buf = []
        elif VERT in s:
            inner = re.sub(r"^\s*" + re.escape(VERT) + r"\s*", "", s)
            inner = re.sub(r"\s*" + re.escape(VERT) + r"\s*$", "", inner).strip()
            if inner:
                buf.append(inner)

    if buf:
        m = re.match(r"^(\d+)\.\s+(.+)$", buf[0])
        if m:
            layers.append({
                "num":        int(m.group(1)),
                "title":      m.group(2).strip(),
                "components": "  \u00b7  ".join(buf[1:]) if len(buf) > 1 else "",
            })

    return layers if len(layers) >= 3 else None


# ── SVG layer stack diagram ────────────────────────────────────────────────────

_LAYER_COLOR_RULES = [
    (["automation", "terraform", "ansible", "intersight"],               "#7c3aed"),
    (["ai platform", "gpu operator", "network operator", "nfd", "mig"],  "#059669"),
    (["container", "kubernetes", "openshift", "scheduling"],             "#dc2626"),
    (["compute", "ucs", "h100", "a100", "nvlink", "nvswitch"],           "#d97706"),
    (["backend", "rdma", "roce", "pfc", "ecn", "nexus"],                 "#2563eb"),
    (["storage", "nvme", "vast", "pure", "parallel"],                    "#0891b2"),
    (["frontend", "client", "ingress", "access", "load balancing"],      "#4f46e5"),
]


def _pick_layer_color(title):
    t = title.lower()
    for keywords, color in _LAYER_COLOR_RULES:
        if any(kw in t for kw in keywords):
            return color
    return "#64748b"


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_stack_svg_html(layers, diagram_title):
    """
    Render layer dicts as a proper SVG stack diagram with colored boxes.
    layers[0] is at top (same visual order as the ASCII art).
    Returns self-contained HTML for Playwright.
    """
    W       = 740
    ROW_H   = 80
    GAP     = 5
    PAD_TOP = 72
    PAD_BOT = 32
    total_h = PAD_TOP + len(layers) * (ROW_H + GAP) + PAD_BOT

    parts = []
    parts.append(
        '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
        'body{margin:0;background:#0f172a;}svg{display:block;}'
        '</style></head><body>'
        '<svg width="%d" height="%d" xmlns="http://www.w3.org/2000/svg">' % (W, total_h)
    )
    parts.append(
        '<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%%" stop-color="#1e293b"/>'
        '<stop offset="100%%" stop-color="#0f172a"/>'
        '</linearGradient></defs>'
        '<rect width="%d" height="%d" fill="url(#bg)"/>' % (W, total_h)
    )
    parts.append(
        '<line x1="0" y1="%d" x2="%d" y2="%d" '
        'stroke="rgba(255,255,255,0.08)" stroke-width="1"/>' % (PAD_TOP - 10, W, PAD_TOP - 10)
    )
    parts.append(
        '<text x="%d" y="46" text-anchor="middle" '
        'font-family="\'Inter\',Arial,sans-serif" '
        'font-size="15" font-weight="800" fill="#e2e8f0">%s</text>' % (W // 2, _esc(diagram_title))
    )

    for i, layer in enumerate(layers):
        y     = PAD_TOP + i * (ROW_H + GAP)
        color = _pick_layer_color(layer["title"])
        label = "Layer %d: %s" % (layer["num"], layer["title"])
        comp  = layer["components"]
        if len(comp) > 90:
            comp = comp[:87] + "\u2026"

        parts.append(
            '<rect x="20" y="%d" width="%d" height="%d" rx="7" fill="%s" fill-opacity="0.88"/>'
            % (y, W - 40, ROW_H, color)
        )
        parts.append(
            '<rect x="20" y="%d" width="7" height="%d" rx="4" fill="white" fill-opacity="0.25"/>'
            % (y, ROW_H)
        )
        parts.append(
            '<text x="42" y="%d" font-family="\'Inter\',Arial,sans-serif" '
            'font-size="14" font-weight="800" fill="#ffffff">%s</text>'
            % (y + 30, _esc(label))
        )
        parts.append(
            '<text x="42" y="%d" font-family="\'Inter\',Arial,sans-serif" '
            'font-size="11" fill="rgba(255,255,255,0.65)">%s</text>'
            % (y + 56, _esc(comp))
        )

    parts.append("</svg></body></html>")
    return "".join(parts)


# ── Architecture block (styled terminal card) ──────────────────────────────────

def build_arch_html(code_text):
    css_path = ASSETS_DIR / "css/diagram.css"
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
        + css
        + ".wrap{padding:24px;}"
        + "</style></head><body>"
        + '<div class="wrap"><div class="diagram"><pre>'
        + _esc(code_text)
        + "</pre></div></div></body></html>"
    )


# ── Markdown table image ───────────────────────────────────────────────────────

def _parse_md_table(md):
    lines = [l.strip() for l in md.strip().split("\n")]
    if len(lines) < 3:
        return None
    headers = [c.strip() for c in lines[0].strip("|").split("|")]
    rows = []
    for line in lines[2:]:
        if "|" in line:
            rows.append([c.strip() for c in line.strip("|").split("|")])
    return headers, rows


def build_table_html(headers, rows):
    th = "".join("<th>%s</th>" % h for h in headers)
    tr = "".join(
        "<tr>" + "".join("<td>%s</td>" % c for c in row) + "</tr>"
        for row in rows
    )
    css = (
        "body{margin:0;background:#0f172a;padding:28px 24px;"
        "font-family:'Inter',Arial,sans-serif;}"
        "table{border-collapse:collapse;width:100%;}"
        "th{background:#1e3a8a;color:#bfdbfe;padding:12px 16px;"
        "text-align:left;font-size:12px;font-weight:700;"
        "letter-spacing:.06em;text-transform:uppercase;}"
        "td{background:#1e293b;color:#cbd5e1;padding:10px 16px;"
        "font-size:13px;border-bottom:1px solid #273548;}"
        "tr:nth-child(even) td{background:#182333;}"
        "th:first-child{border-radius:8px 0 0 0;}"
        "th:last-child{border-radius:0 8px 0 0;}"
    )
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
        + css
        + "</style></head>"
        + "<body><table><thead><tr>" + th + "</tr></thead><tbody>" + tr + "</tbody></table></body></html>"
    )


# ── Pillow gradient cover image ────────────────────────────────────────────────

_TAG_COLORS = {
    "AI":         (99,  102, 241),
    "Kubernetes": (220,  38,  38),
    "Cloud":      ( 14, 165, 233),
    "Automation": (234, 179,   8),
    "SAP":        ( 34, 197,  94),
}


def _load_font(size):
    from PIL import ImageFont
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def generate_pillow_cover(title, tags, output_path):
    """
    1200x630 OG-format cover — dark gradient + title + tag badges.
    No LLM or external API required.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("  Pillow not installed -- run: pip install Pillow")
        return False

    W, H = 1200, 630
    img  = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)

    top_c, bot_c = (30, 41, 59), (15, 23, 42)
    for y in range(H):
        t = y / H
        draw.line([(0, y), (W, y)],
                  fill=tuple(int(top_c[i] * (1 - t) + bot_c[i] * t) for i in range(3)))

    for x in range(0, W, 60):
        draw.line([(x, 0), (x, H)], fill=(22, 33, 50), width=1)
    for y in range(0, H, 60):
        draw.line([(0, y), (W, y)], fill=(22, 33, 50), width=1)

    accent = _TAG_COLORS.get(tags[0] if tags else "", (99, 102, 241))
    draw.rectangle([0, 0, 8, H], fill=accent)

    title_font = _load_font(52)
    tag_font   = _load_font(22)
    site_font  = _load_font(18)

    y_text = 160
    for line in textwrap.wrap(title, width=36):
        draw.text((64, y_text), line, font=title_font, fill=(226, 232, 240))
        bbox    = draw.textbbox((0, 0), line, font=title_font)
        y_text += (bbox[3] - bbox[1]) + 14

    x_badge, y_badge = 64, y_text + 28
    for tag in tags[:4]:
        color = _TAG_COLORS.get(tag, (100, 116, 139))
        label = "#%s" % tag
        bbox  = draw.textbbox((0, 0), label, font=tag_font)
        tw    = bbox[2] - bbox[0]
        x1, y1 = x_badge - 10, y_badge - 4
        x2, y2 = x_badge + tw + 10, y_badge + 30
        try:
            draw.rounded_rectangle([x1, y1, x2, y2], radius=5, fill=color)
        except AttributeError:
            draw.rectangle([x1, y1, x2, y2], fill=color)
        draw.text((x_badge, y_badge), label, font=tag_font, fill=(255, 255, 255))
        x_badge += tw + 26

    draw.text((64, H - 48), "maheshpabba.com", font=site_font, fill=(71, 85, 105))

    COVER_DIR.mkdir(parents=True, exist_ok=True)
    img.save(str(output_path), "JPEG", quality=92)
    return True


# ── DALL-E 3 cover image ───────────────────────────────────────────────────────

def generate_dalle_cover(title, tags, output_path):
    """
    Photo-realistic cover via DALL-E 3.
    THIS is the only path that requires an LLM / image generation model.
    Requires: pip install openai  +  OPENAI_API_KEY env var.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("  OPENAI_API_KEY not set -- skipping DALL-E cover")
        return False
    try:
        from openai import OpenAI
    except ImportError:
        print("  openai not installed -- run: pip install openai")
        return False

    client = OpenAI(api_key=api_key)
    prompt = (
        "A professional, photorealistic digital illustration for a tech blog post "
        "titled '%s'. Topics: %s. Style: dark-themed enterprise datacenter, "
        "futuristic GPU server racks with glowing blue network cables, clean lines, "
        "16:9 widescreen, cinematic lighting, no text overlays."
    ) % (title, ", ".join(tags))

    print("    Calling DALL-E 3 for '%s'..." % output_path.name)
    try:
        resp = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1792x1024",
            quality="standard",
            n=1,
        )
        url = resp.data[0].url
        if not url.startswith("https://"):
            print("  Unexpected URL scheme -- skipping")
            return False
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "gen_diagram/2.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        COVER_DIR.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)
        return True
    except Exception as e:
        print("  DALL-E error: %s" % e)
        return False


# ── Post scanner ───────────────────────────────────────────────────────────────

def scan_posts(filter_slug=None):
    """Scan posts/*.md, detect visual patterns, return list of post dicts."""
    posts = []
    for md_path in sorted(POSTS_DIR.glob("*.md")):
        slug = md_path.stem
        if filter_slug and filter_slug not in slug:
            continue
        text        = md_path.read_text(encoding="utf-8")
        fm          = parse_frontmatter(text)
        body        = post_body(text)
        code_blocks = find_code_blocks(body)
        md_tables   = find_md_tables(body)

        stack_layers = None
        arch_blocks  = []
        for block in code_blocks:
            if has_box_chars(block["content"]):
                parsed = parse_layer_stack(block["content"])
                if parsed:
                    stack_layers = parsed
                else:
                    arch_blocks.append(block["content"])

        tags = [t.strip() for t in str(fm.get("tags", "")).split(",") if t.strip()]
        posts.append({
            "slug":         slug,
            "title":        fm.get("title", slug),
            "tags":         tags,
            "stack_layers": stack_layers,
            "arch_blocks":  arch_blocks,
            "md_tables":    md_tables,
        })
    return posts


# ── Playwright renderer ────────────────────────────────────────────────────────

def render_html_to_png(page, html, out_path, width=760):
    page.set_viewport_size({"width": width, "height": 600})
    page.set_content(html, wait_until="networkidle")
    height = page.evaluate("document.body.scrollHeight")
    page.set_viewport_size({"width": width, "height": max(height, 100)})
    page.screenshot(path=str(out_path), full_page=True)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Dynamic visual image generator for blog posts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Image types:
  diagrams (default)  Real SVG visuals  -- no LLM needed
  --covers            Gradient cover     -- Pillow, no LLM, pip install Pillow
  --ai-covers         Photo-realistic   -- DALL-E 3, needs OPENAI_API_KEY
        """,
    )
    parser.add_argument("--post",      help="Only process posts whose slug contains this string")
    parser.add_argument("--covers",    action="store_true",
                        help="Generate gradient cover images via Pillow (no LLM)")
    parser.add_argument("--ai-covers", action="store_true",
                        help="Generate covers via DALL-E 3 (requires OPENAI_API_KEY)")
    args = parser.parse_args()

    posts = scan_posts(args.post)
    if not posts:
        print("No matching posts found.")
        return

    print("\nScanned %d post(s).\n" % len(posts))

    has_diagrams = any(
        p["stack_layers"] or p["arch_blocks"] or p["md_tables"] for p in posts
    )

    if has_diagrams:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("Playwright not installed -- run: pip install playwright && python -m playwright install chromium")
            sys.exit(1)

        print("Generating diagrams...\n")
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page    = browser.new_page()
            for post in posts:
                slug    = post["slug"]
                emitted = []

                if post["stack_layers"]:
                    out  = DIAG_DIR / ("%s-stack.png" % slug)
                    html = build_stack_svg_html(post["stack_layers"], post["title"])
                    render_html_to_png(page, html, out, width=760)
                    emitted.append("stack diagram  -> %s" % out.name)

                for i, block in enumerate(post["arch_blocks"]):
                    out  = DIAG_DIR / ("%s-arch-%d.png" % (slug, i + 1))
                    html = build_arch_html(block)
                    render_html_to_png(page, html, out, width=760)
                    emitted.append("arch block %d  -> %s" % (i + 1, out.name))

                for i, table_md in enumerate(post["md_tables"]):
                    parsed = _parse_md_table(table_md)
                    if parsed:
                        headers, rows = parsed
                        out  = DIAG_DIR / ("%s-table-%d.png" % (slug, i + 1))
                        html = build_table_html(headers, rows)
                        render_html_to_png(page, html, out, width=820)
                        emitted.append("table %d       -> %s" % (i + 1, out.name))

                if emitted:
                    print("  [%s]" % slug)
                    for e in emitted:
                        print("    OK  %s" % e)
                else:
                    print("  [%s] -- no visual patterns detected" % slug)
            browser.close()

    if args.covers or args.ai_covers:
        print("\nGenerating cover images...\n")
        for post in posts:
            slug       = post["slug"]
            cover_path = COVER_DIR / ("%s.jpg" % slug)
            if args.ai_covers:
                print("  [%s] DALL-E 3..." % slug)
                ok = generate_dalle_cover(post["title"], post["tags"], cover_path)
            else:
                print("  [%s] Pillow gradient..." % slug)
                ok = generate_pillow_cover(post["title"], post["tags"], cover_path)
            if ok:
                print("    OK  %s" % cover_path)

    print("\nDone.\n")


if __name__ == "__main__":
    main()
