#!/usr/bin/env python3
"""
generate.py — Blog post generator for maheshpabba.github.io
Usage:
    python generate.py              # generate all posts
    python generate.py --new        # scaffold a new post interactively
    python generate.py --watch      # watch posts/ for changes and regenerate

Reads:  posts/*.md
Writes: posts/*.html  (static, with full OG meta tags for LinkedIn/Twitter sharing)
Updates: posts/index.json
"""

import json
import re
import sys
import time
import argparse
import datetime
from pathlib import Path

try:
    import markdown
except ImportError:
    sys.exit("Missing dependency: run  pip install -r requirements.txt")

POSTS_DIR  = Path(__file__).parent / "posts"
ASSETS_DIR = Path(__file__).parent / "assets"
BASE_URL   = "https://maheshpabba.com"  # update when custom domain is live


def _load(rel_path: str) -> str:
    """Load a file from the assets/ directory."""
    return (ASSETS_DIR / rel_path).read_text(encoding="utf-8")


# Load shared HTML fragments once at startup
NAVBAR_POST   = _load("html/_navbar_post.html")   # used in generated blog posts
FOOTER_POST   = _load("html/_footer_post.html")   # used in generated blog posts


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """
    Parse optional YAML-style frontmatter block at top of markdown.
    Supports simple key: value pairs only (no nested YAML).

    Example frontmatter:
    ---
    title: My Post Title
    date: 2026-05-20
    tags: AI, Kubernetes
    excerpt: Short description
    image: ../assets/images/blog/blog-post-thumb-card-1.jpg
    ---
    """
    meta = {}
    body = content

    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            fm_block = content[3:end].strip()
            body = content[end + 4:].strip()
            for line in fm_block.splitlines():
                if ":" in line:
                    key, _, val = line.partition(":")
                    meta[key.strip()] = val.strip()

    # Also parse inline header-style metadata: > **Author:** ...
    if not meta and content.startswith(">"):
        first_line = content.splitlines()[0]
        for part in first_line.split("|"):
            part = part.strip().lstrip(">").strip()
            part = re.sub(r"\*\*(.+?)\*\*", r"\1", part)  # remove bold markers
            if ":" in part:
                k, _, v = part.partition(":")
                meta[k.strip().lower()] = v.strip()

    return meta, body


def extract_title(md_content: str) -> str:
    """Extract H1 title from markdown body."""
    for line in md_content.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return "Untitled Post"


def extract_excerpt(html_content: str, length: int = 200) -> str:
    """Strip HTML tags and return plain text excerpt."""
    text = re.sub(r"<[^>]+>", "", html_content)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > length:
        text = text[:length].rsplit(" ", 1)[0] + "…"
    return text


def slug_from_filename(filename: str) -> str:
    return Path(filename).stem


def generate_html(md_path: Path, index_entry: dict | None = None) -> dict:
    """Convert a .md file to a styled .html file. Returns metadata dict for index."""
    content = md_path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(content)

    slug = slug_from_filename(md_path.name)
    title = meta.get("title") or extract_title(body)
    date = meta.get("date", slug[:10] if len(slug) >= 10 else datetime.date.today().isoformat())
    tags = [t.strip() for t in meta.get("tags", "").split(",") if t.strip()]
    image_raw = meta.get("image", "assets/images/blog/blog-post-thumb-card-1.jpg")
    # Posts live in posts/, so relative paths need ../  prepended
    if not image_raw.startswith(("http", "../", "/")):
        image_raw = image_raw.lstrip("./")
        image_raw = "../" + image_raw
    image = image_raw
    excerpt_override = meta.get("excerpt", "")

    # Convert markdown to HTML
    md = markdown.Markdown(extensions=["fenced_code", "tables", "toc", "codehilite"])
    html_body = md.convert(body)

    # Strip the leading <h1> from the body — the title is already in the post header section
    html_body = re.sub(r'^\s*<h1[^>]*>.*?</h1>\s*', '', html_body, count=1, flags=re.DOTALL)

    excerpt = excerpt_override or extract_excerpt(html_body)

    tags_html = " ".join(f'<span class="badge badge-ghost badge-sm">{t}</span>' for t in tags)
    year = datetime.date.today().year

    page_html = f"""<!DOCTYPE html>
<html lang="en" data-theme="solarized-light">
<head>
  <meta charset="utf-8">
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="description" content="{excerpt}">
  <meta name="author" content="Mahesh Pabba">

  <!-- Open Graph (LinkedIn / Twitter preview) -->
  <meta property="og:title" content="{title}">
  <meta property="og:description" content="{excerpt}">
  <meta property="og:image" content="{BASE_URL}/{image.lstrip('../')}">
  <meta property="og:type" content="article">
  <meta property="og:url" content="{BASE_URL}/posts/{slug}.html">
  <meta property="article:published_time" content="{date}">
  <meta property="article:author" content="Mahesh Pabba">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{title}">
  <meta name="twitter:description" content="{excerpt}">
  <meta name="twitter:image" content="{BASE_URL}/{image.lstrip('../')}">

  <link rel="shortcut icon" href="../favicon.ico">
  <link href="https://cdn.jsdelivr.net/npm/daisyui@4.12.10/dist/full.min.css" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <script defer src="../assets/fontawesome/js/all.js"></script>
  <link rel="stylesheet" href="../assets/css/site.css">
  <link rel="stylesheet" href="../assets/css/blog-post.css">
  <link rel="stylesheet" href="../assets/plugins/highlight/styles/monokai-sublime.css">
  <script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/highlight.min.js"></script>
</head>
<body class="min-h-screen bg-base-100">

{NAVBAR_POST}

<!-- Hero banner -->
<div class="w-full h-64 overflow-hidden relative">
  <img src="{image}" alt="{title}" class="w-full h-full object-cover opacity-40">
  <div class="absolute inset-0 bg-gradient-to-t from-base-100 to-transparent"></div>
</div>

<div class="max-w-3xl mx-auto px-4 pb-20 -mt-12 relative">

  <!-- Post header -->
  <div class="mb-10">
    <div class="flex flex-wrap gap-2 mb-4">{tags_html}</div>
    <h1 class="text-3xl lg:text-4xl font-bold leading-tight mb-4">{title}</h1>
    <div class="flex items-center gap-4 text-sm text-base-content/40">
      <span><i class="fas fa-user mr-1"></i> Mahesh Pabba</span>
      <span><i class="fas fa-calendar mr-1"></i> {date}</span>
      <a href="../blog-home.html" class="link link-hover ml-auto"><i class="fas fa-arrow-left mr-1"></i> All posts</a>
    </div>
    <div class="divider"></div>
  </div>

  <!-- Article body -->
  <article class="prose max-w-none">
    {html_body}
  </article>

  <!-- Share section -->
  <div class="divider mt-12"></div>
  <div class="flex flex-col sm:flex-row items-center justify-between gap-4">
    <div>
      <p class="font-semibold mb-2">Share this post</p>
      <div class="flex gap-2">
        <a href="https://www.linkedin.com/sharing/share-offsite/?url={BASE_URL}/posts/{slug}.html"
           target="_blank" rel="noopener"
           class="btn btn-outline btn-sm gap-2">
          <i class="fab fa-linkedin-in"></i> LinkedIn
        </a>
        <a href="https://twitter.com/intent/tweet?url={BASE_URL}/posts/{slug}.html&text={title}"
           target="_blank" rel="noopener"
           class="btn btn-ghost btn-sm gap-2">
          <i class="fab fa-twitter"></i> Twitter/X
        </a>
      </div>
    </div>
    <a href="../blog-home.html" class="btn btn-primary btn-sm">
      <i class="fas fa-arrow-left mr-1"></i> More posts
    </a>
  </div>

  <!-- Author card -->
  <div class="card bg-base-200 mt-10">
    <div class="card-body flex-row gap-5 items-center p-6">
      <div class="w-16 h-16 rounded-full overflow-hidden shrink-0">
        <img src="../assets/images/profile-lg.jpg" alt="Mahesh Pabba" class="w-full h-full object-cover object-center">
      </div>
      <div>
        <p class="font-bold">Mahesh Pabba</p>
        <p class="text-sm text-base-content/60">AI &amp; Cloud Architect · Cisco · 22+ years in enterprise infrastructure</p>
        <div class="flex gap-2 mt-2">
          <a href="https://www.linkedin.com/in/maheshpabba" target="_blank" rel="noopener" class="btn btn-ghost btn-xs"><i class="fab fa-linkedin-in"></i></a>
          <a href="https://github.com/maheshpabba" target="_blank" rel="noopener" class="btn btn-ghost btn-xs"><i class="fab fa-github"></i></a>
          <a href="mailto:mahesh.pabba@gmail.com" class="btn btn-ghost btn-xs"><i class="fas fa-envelope"></i></a>
        </div>
      </div>
    </div>
  </div>

</div>

{FOOTER_POST.replace("__YEAR__", str(year))}

<script>hljs.highlightAll();</script>
</body>
</html>"""

    out_path = md_path.with_suffix(".html")
    out_path.write_text(page_html, encoding="utf-8")
    print(f"  ✓ Generated: {out_path.name}")

    return {
        "slug": slug,
        "title": title,
        "date": date,
        "excerpt": excerpt,
        "tags": tags,
        "image": image.replace("../", ""),
    }


def update_index(posts: list[dict]):
    """Write posts/index.json sorted newest first."""
    posts_sorted = sorted(posts, key=lambda p: p["date"], reverse=True)
    index_path = POSTS_DIR / "index.json"
    index_path.write_text(json.dumps(posts_sorted, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  ✓ Updated: posts/index.json ({len(posts_sorted)} posts)")


TAG_COLORS = {
    "AI": "badge-primary",
    "Kubernetes": "badge-secondary",
    "Cloud": "badge-info",
    "SAP": "badge-success",
    "Automation": "badge-warning",
}


def _post_card(post: dict) -> str:
    """Render a single post card as static HTML."""
    tags_html = " ".join(
        f'<span class="badge badge-sm {TAG_COLORS.get(t, "badge-ghost")}">{t}</span>'
        for t in post.get("tags", [])
    )
    img = post.get("image", "assets/images/profile-lg.jpg")
    return f"""
    <div class="card bg-base-200 shadow hover:shadow-lg transition-shadow" data-tags="{",".join(post.get("tags", []))}">
      <a href="posts/{post["slug"]}.html" class="block">
        <figure class="h-44 overflow-hidden bg-base-300">
          <img src="{img}" alt="{post["title"]}"
               class="w-full h-full object-cover transition-opacity">
        </figure>
      </a>
      <div class="card-body p-5">
        <div class="flex flex-wrap gap-1 mb-2">{tags_html}</div>
        <h2 class="card-title text-base leading-snug">
          <a href="posts/{post["slug"]}.html" class="hover:text-primary transition-colors">{post["title"]}</a>
        </h2>
        <p class="text-sm text-base-content/60 line-clamp-3">{post["excerpt"]}</p>
        <div class="card-actions justify-between items-center mt-3">
          <span class="text-xs text-base-content/40"><i class="fas fa-calendar mr-1"></i>{post["date"]}</span>
          <a href="posts/{post["slug"]}.html" class="btn btn-primary btn-xs">Read &rarr;</a>
        </div>
      </div>
    </div>"""


def generate_blog_home(posts: list[dict]):
    """Regenerate blog-home.html with post cards baked in as static HTML."""
    posts_sorted = sorted(posts, key=lambda p: p["date"], reverse=True)

    # Collect all unique tags across posts for filter buttons
    all_tags = []
    seen = set()
    for p in posts_sorted:
        for t in p.get("tags", []):
            if t not in seen:
                seen.add(t)
                all_tags.append(t)

    tag_buttons = '\n    '.join(
        f'<button class="btn btn-outline btn-sm tag-btn" data-tag="{t}">{t}</button>'
        for t in all_tags
    )

    cards_html = "\n".join(_post_card(p) for p in posts_sorted)
    empty_msg = "" if posts_sorted else '<p class="text-base-content/40 py-20 text-center col-span-3">No posts yet. Check back soon!</p>'
    year = datetime.date.today().year

    navbar = _load("html/_navbar.html")
    footer = _load("html/_footer.html").replace("__YEAR__", str(year))

    html = f"""<!DOCTYPE html>
<html lang="en" data-theme="night">
<head>
  <title>Blog — Mahesh Pabba</title>
  <meta charset="utf-8">
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="description" content="Articles on AI infrastructure, Kubernetes, SAP, cloud architecture and automation by Mahesh Pabba.">
  <meta name="author" content="Mahesh Pabba">
  <meta property="og:title" content="Blog — Mahesh Pabba">
  <meta property="og:description" content="Articles on AI infrastructure, Kubernetes, SAP, cloud architecture and automation.">
  <meta property="og:image" content="assets/images/profile-lg.jpg">
  <link rel="shortcut icon" href="favicon.ico">
  <link href="https://cdn.jsdelivr.net/npm/daisyui@4.12.10/dist/full.min.css" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <script defer src="assets/fontawesome/js/all.js"></script>
  <link rel="stylesheet" href="assets/css/site.css">
</head>
<body class="min-h-screen bg-base-100">

{navbar}

<!-- HERO -->
<section class="py-16 bg-base-200">
  <div class="max-w-3xl mx-auto px-4 text-center">
    <h1 class="text-4xl font-bold tracking-tight mb-3">Blog</h1>
    <p class="text-base-content/60 text-lg">Thoughts on AI infrastructure, Kubernetes, SAP, cloud, and automation — from the field.</p>
  </div>
</section>

<!-- TAG FILTER -->
<div class="max-w-5xl mx-auto px-4 pt-10">
  <div class="flex flex-wrap gap-2" id="tag-filters">
    <button class="btn btn-primary btn-sm tag-btn" data-tag="all">All</button>
    {tag_buttons}
  </div>
</div>

<!-- POST GRID -->
<div class="max-w-5xl mx-auto px-4 py-10 pb-20">
  <div id="posts-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
    {cards_html}
    {empty_msg}
  </div>
</div>

{footer}

<script src="assets/js/tag-filter.js"></script>
</body>
</html>"""

    out = Path(__file__).parent / "blog-home.html"
    out.write_text(html, encoding="utf-8")
    print(f"  ✓ Regenerated: blog-home.html ({len(posts_sorted)} posts)")


def generate_all():
    """Generate HTML for all .md files in posts/ and update index."""
    md_files = sorted(POSTS_DIR.glob("*.md"))
    if not md_files:
        print("No .md files found in posts/")
        return

    print(f"\nGenerating {len(md_files)} post(s)...")
    posts = []
    for md in md_files:
        try:
            meta = generate_html(md)
            posts.append(meta)
        except Exception as e:
            print(f"  ✗ Error processing {md.name}: {e}")

    update_index(posts)
    generate_blog_home(posts)
    print("\nDone! Push to GitHub to publish.\n")


def scaffold_new_post():
    """Interactively create a new blank .md post."""
    print("\n── New Blog Post ──────────────────────")
    title = input("Post title: ").strip()
    tags = input("Tags (comma-separated, e.g. AI, Kubernetes): ").strip()
    excerpt = input("Short excerpt (1-2 sentences): ").strip()

    today = datetime.date.today().isoformat()
    slug_base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = f"{today}-{slug_base}"
    filename = POSTS_DIR / f"{slug}.md"

    template = f"""---
title: {title}
date: {today}
tags: {tags}
excerpt: {excerpt}
image: ../assets/images/blog/blog-post-thumb-card-1.jpg
---

# {title}

Write your post here using standard Markdown.

## Introduction

...

## Section 2

...

## Conclusion

...

---

*Have questions? [Reach out](../contact.html)*
"""
    filename.write_text(template, encoding="utf-8")
    print(f"\n✓ Created: {filename}")
    print(f"  Edit it, then run:  python generate.py\n")


def watch_and_regenerate():
    """Watch posts/ directory and regenerate on .md changes."""
    print("Watching posts/ for changes... (Ctrl+C to stop)\n")
    mtimes = {}

    while True:
        changed = False
        for md in POSTS_DIR.glob("*.md"):
            mtime = md.stat().st_mtime
            if mtimes.get(md) != mtime:
                mtimes[md] = mtime
                if changed is False:
                    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Change detected, regenerating...")
                changed = True

        if changed:
            generate_all()

        time.sleep(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Blog post generator")
    parser.add_argument("--new", action="store_true", help="Scaffold a new blog post")
    parser.add_argument("--watch", action="store_true", help="Watch for changes and regenerate")
    args = parser.parse_args()

    if args.new:
        scaffold_new_post()
    elif args.watch:
        watch_and_regenerate()
    else:
        generate_all()
