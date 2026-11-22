"""Self-contained offline HTML Visual Inventory Board generator (zero external CDNs)."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentsync.adapters.base import ToolAdapter
from agentsync.utils.atomic_io import atomic_write_text


def _classify_status(present_hashes: dict[str, str], eligible_count: int) -> str:
    if len(present_hashes) <= 1 and eligible_count > 1:
        return "one-host-only"
    unique = set(present_hashes.values())
    if len(unique) == 1 and len(present_hashes) >= eligible_count:
        return "in-sync"
    if len(unique) > 1:
        return "conflict"
    return "drift"


def collect_inventory_rows(adapters: list[ToolAdapter]) -> list[dict[str, Any]]:
    """Collect cross-tool inventory rows across skills, MCP, plugins, instructions, projects, and chats."""
    rows: list[dict[str, Any]] = []

    # 1. Skills
    skill_adapters = [a for a in adapters if a.capabilities.skills]
    skill_maps = {a.tool_name: a.read_skills() for a in skill_adapters}
    for name in sorted({k for m in skill_maps.values() for k in m}):
        hashes = {t: m[name].content_hash[:10] for t, m in skill_maps.items() if name in m}
        rows.append(
            {
                "category": "skills",
                "name": name,
                "status": _classify_status(hashes, len(skill_adapters)),
                "tools": sorted(hashes.keys()),
                "hashes": hashes,
            }
        )

    # 2. MCP Servers
    mcp_adapters = [a for a in adapters if a.capabilities.mcp]
    mcp_maps = {a.tool_name: a.read_mcp_servers() for a in mcp_adapters}
    for name in sorted({k for m in mcp_maps.values() for k in m}):
        hashes = {t: m[name].content_hash[:10] for t, m in mcp_maps.items() if name in m}
        rows.append(
            {
                "category": "mcp",
                "name": name,
                "status": _classify_status(hashes, len(mcp_adapters)),
                "tools": sorted(hashes.keys()),
                "hashes": hashes,
            }
        )

    # 3. Plugins
    pl_adapters = [a for a in adapters if a.capabilities.plugins]
    pl_maps = {a.tool_name: a.read_plugins() for a in pl_adapters}
    for name in sorted({k for m in pl_maps.values() for k in m}):
        hashes = {t: m[name].content_hash[:10] for t, m in pl_maps.items() if name in m}
        rows.append(
            {
                "category": "plugins",
                "name": name,
                "status": _classify_status(hashes, len(pl_adapters)),
                "tools": sorted(hashes.keys()),
                "hashes": hashes,
            }
        )

    # 4. Global Instructions
    inst_adapters = [a for a in adapters if a.capabilities.instructions]
    inst_hashes: dict[str, str] = {}
    for a in inst_adapters:
        doc = a.read_global_instructions()
        if doc is not None:
            inst_hashes[a.tool_name] = doc.content_hash[:10]
    if inst_hashes:
        rows.append(
            {
                "category": "instructions",
                "name": "global",
                "status": _classify_status(inst_hashes, len(inst_adapters)),
                "tools": sorted(inst_hashes.keys()),
                "hashes": inst_hashes,
            }
        )

    # 5. Projects
    proj_adapters = [a for a in adapters if a.capabilities.projects]
    proj_maps = {a.tool_name: a.read_projects() for a in proj_adapters}
    for key in sorted({k for m in proj_maps.values() for k in m}):
        hashes = {t: m[key].name for t, m in proj_maps.items() if key in m}
        display = next(m[key].name for m in proj_maps.values() if key in m)
        rows.append(
            {
                "category": "projects",
                "name": display,
                "status": _classify_status(hashes, len(proj_adapters)),
                "tools": sorted(hashes.keys()),
                "hashes": hashes,
            }
        )

    # 6. Chats (grouped by fingerprint)
    chat_adapters = [a for a in adapters if a.capabilities.chats]
    chat_by_fp: dict[str, dict[str, tuple[str, str]]] = {}
    for a in chat_adapters:
        pmap = proj_maps.get(a.tool_name) or {}
        for c in a.read_chats(pmap):
            chat_by_fp.setdefault(c.fingerprint, {})[a.tool_name] = (c.title or c.native_id, c.content_hash[:10])
    for fp, tmap in sorted(chat_by_fp.items()):
        title = next(iter(tmap.values()))[0]
        hashes = {t: h for t, (_, h) in tmap.items()}
        rows.append(
            {
                "category": "chats",
                "name": f"{title[:60]} ({fp[:6]})",
                "status": _classify_status(hashes, len(chat_adapters)),
                "tools": sorted(hashes.keys()),
                "hashes": hashes,
            }
        )

    return rows


def generate_inventory_board(adapters: list[ToolAdapter], output_path: Path) -> tuple[Path, int]:
    """Render a completely offline, filterable dark-mode HTML inventory dashboard."""
    rows = collect_inventory_rows(adapters)
    tools = [a.tool_name for a in adapters]
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    counts = {
        "total": len(rows),
        "in-sync": sum(1 for r in rows if r["status"] == "in-sync"),
        "drift": sum(1 for r in rows if r["status"] == "drift"),
        "conflict": sum(1 for r in rows if r["status"] == "conflict"),
        "one-host-only": sum(1 for r in rows if r["status"] == "one-host-only"),
    }

    safe_json = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    safe_tools = html.escape(", ".join(tools) or "none")

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AgentSync Offline Visual Inventory Board</title>
<style>
  :root {{ --bg:#0d1117; --card:#161b22; --border:#30363d; --text:#e6edf3; --muted:#8b949e;
           --sync:#3fb950; --drift:#d29922; --conflict:#f85149; --only:#58a6ff; --accent:#1f6feb; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--text); padding:24px; }}
  header {{ display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:12px; border-bottom:1px solid var(--border); padding-bottom:16px; margin-bottom:20px; }}
  h1 {{ margin:0; font-size:22px; letter-spacing:0.5px; }}
  .meta {{ color:var(--muted); font-size:13px; }}
  .stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-bottom:20px; }}
  .stat {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px; }}
  .stat .num {{ font-size:26px; font-weight:700; }}
  .stat .lbl {{ font-size:12px; color:var(--muted); text-transform:uppercase; }}
  .controls {{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:16px; align-items:center; }}
  input {{ background:var(--card); border:1px solid var(--border); color:var(--text); padding:8px 12px; border-radius:8px; min-width:260px; }}
  button {{ background:var(--card); border:1px solid var(--border); color:var(--text); padding:7px 12px; border-radius:8px; cursor:pointer; font-size:13px; }}
  button.active {{ background:var(--accent); border-color:var(--accent); }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--border); border-radius:10px; overflow:hidden; }}
  th, td {{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--border); font-size:13px; }}
  th {{ color:var(--muted); font-weight:600; background:#12171f; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; font-weight:600; }}
  .in-sync {{ background:rgba(63,185,80,.18); color:var(--sync); }}
  .drift {{ background:rgba(210,153,34,.18); color:var(--drift); }}
  .conflict {{ background:rgba(248,81,73,.18); color:var(--conflict); }}
  .one-host-only {{ background:rgba(88,166,255,.18); color:var(--only); }}
  .pill {{ display:inline-block; background:#21262d; border:1px solid var(--border); border-radius:6px; padding:2px 6px; margin-right:4px; font-size:11px; }}
</style>
</head>
<body>
<header>
  <div>
    <h1>AGENTSYNC Inventory Board</h1>
    <div class="meta">Tools: {safe_tools} &middot; Generated {html.escape(generated_at)} &middot; Offline Zero-Dependency Snapshot</div>
  </div>
</header>
<section class="stats">
  <div class="stat"><div class="num">{counts['total']}</div><div class="lbl">Total Entities</div></div>
  <div class="stat"><div class="num" style="color:var(--sync)">{counts['in-sync']}</div><div class="lbl">In Sync</div></div>
  <div class="stat"><div class="num" style="color:var(--drift)">{counts['drift']}</div><div class="lbl">Drift</div></div>
  <div class="stat"><div class="num" style="color:var(--conflict)">{counts['conflict']}</div><div class="lbl">Conflict</div></div>
  <div class="stat"><div class="num" style="color:var(--only)">{counts['one-host-only']}</div><div class="lbl">One Host Only</div></div>
</section>
<div class="controls">
  <input id="q" type="search" placeholder="Filter by name, category, or tool..." oninput="render()">
  <button class="active" onclick="setStatus('all',this)">All</button>
  <button onclick="setStatus('in-sync',this)">In Sync</button>
  <button onclick="setStatus('drift',this)">Drift</button>
  <button onclick="setStatus('conflict',this)">Conflict</button>
  <button onclick="setStatus('one-host-only',this)">One Host Only</button>
</div>
<table>
  <thead><tr><th>Category</th><th>Entity</th><th>Status</th><th>Present On</th></tr></thead>
  <tbody id="rows"></tbody>
</table>
<script>
const DATA = {safe_json};
let statusFilter = 'all';
function esc(s){{ return String(s).replace(/[&<>"']/g, m => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[m])); }}
function setStatus(s, btn){{
  statusFilter = s;
  document.querySelectorAll('.controls button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  render();
}}
function render(){{
  const q = document.getElementById('q').value.trim().toLowerCase();
  const body = document.getElementById('rows');
  const filtered = DATA.filter(r => {{
    if (statusFilter !== 'all' && r.status !== statusFilter) return false;
    if (!q) return true;
    return r.name.toLowerCase().includes(q) || r.category.toLowerCase().includes(q) || r.tools.join(' ').toLowerCase().includes(q);
  }});
  body.innerHTML = filtered.map(r => `<tr>
    <td><strong>${{esc(r.category)}}</strong></td>
    <td>${{esc(r.name)}}</td>
    <td><span class="badge ${{esc(r.status)}}">${{esc(r.status)}}</span></td>
    <td>${{r.tools.map(t => `<span class="pill">${{esc(t)}}</span>`).join('')}}</td>
  </tr>`).join('');
}}
render();
</script>
</body>
</html>
"""
    atomic_write_text(output_path, page)
    return output_path, len(rows)
