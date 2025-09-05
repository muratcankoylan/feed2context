import asyncio
import os
import sys
import json
from datetime import datetime
from typing import Optional, Dict, Any

# Add the parent directory to the path so we can import browser_use
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
load_dotenv()

from pydantic import BaseModel
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from groq import Groq
from fastapi.responses import JSONResponse, HTMLResponse


def _append_jsonl(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


# Browser Use removed in this flow


def _groq() -> Optional[Groq]:
    key = os.getenv('GROQ_API_KEY')
    if not key:
        return None
    # Use latest versions
    return Groq(default_headers={"Groq-Model-Version": "latest"}, api_key=key)


# 1) Extract post content with Compound Mini (supports LinkedIn and X)
EXTRACTOR_SYSTEM = """You are PostExtractor. Visit the given social post URL (LinkedIn or X) and return ONLY the main post text.
Input: {"post_url": "..."}
Rules:
- Return ONLY JSON: {"post_text": "..."}
- Exclude reactions, counts, and comments; include text from 'see more' / collapsed content if applicable
- If the page is not directly accessible, infer the gist from any preview/snippet and user-visible text
- No markdown, no extra text
"""


def _extract_post_with_kimi(post_url: str) -> Optional[str]:
    client = _groq()
    if not client:
        return None
    try:
        resp = client.chat.completions.create(
            model='groq/compound-mini',  # use Compound Mini for URL extraction
            messages=[
                {"role": "system", "content": EXTRACTOR_SYSTEM},
                {"role": "user", "content": json.dumps({"post_url": post_url}, ensure_ascii=False)},
            ],
            temperature=0.0,
        )
        content = (resp.choices[0].message.content or '').strip()
        data = json.loads(content)
        if isinstance(data, dict) and isinstance(data.get('post_text'), str):
            return data['post_text']
    except Exception:
        return None
    return None


# 2) Build a query from extracted post_text + user_note
QUERY_BUILDER_SYSTEM = """You are QueryBuilder. Build ONE research query for Groq Compound.
Inputs:
- post_text: extracted social post text (LinkedIn or X)
- user_note: user's intent
Process:
- Identify entities and intent from post_text and user_note
- Compose a single concise research query (<= 50 words)
Output:
Return ONLY JSON: {"query": "..."}
No markdown or extra text.
"""


def _shape_query_with_kimi(post_text: str, user_note: str) -> Optional[str]:
    client = _groq()
    if not client:
        return None
    try:
        resp = client.chat.completions.create(
            model='moonshotai/kimi-k2-instruct',
            messages=[
                {"role": "system", "content": QUERY_BUILDER_SYSTEM},
                {"role": "user", "content": json.dumps({"post_text": post_text, "user_note": user_note}, ensure_ascii=False)},
            ],
            temperature=0.2,
        )
        content = (resp.choices[0].message.content or '').strip()
        data = json.loads(content)
        if isinstance(data, dict) and isinstance(data.get('query'), str):
            return data['query']
    except Exception:
        return None
    return None


def _compound_search(query: str) -> str:
    client = _groq()
    if not client:
        return ""
    try:
        chunks = client.chat.completions.create(
            model='groq/compound',
            messages=[{"role": "user", "content": query}],
            temperature=0.5,
            max_completion_tokens=2048,
            top_p=1,
            stream=True,
        )
        buf = []
        for chunk in chunks:
            delta = getattr(chunk.choices[0].delta, 'content', None)
            if delta:
                buf.append(delta)
        return ''.join(buf)
    except Exception:
        return ""


api = FastAPI(title='LinkedIn Direct Analyzer')
api.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)


class Trigger(BaseModel):
    url: str
    note: str


def _detect_source(url: str) -> str:
    u = (url or '').lower()
    if 'linkedin.com' in u:
        return 'linkedin'
    if 'x.com' in u or 'twitter.com' in u:
        return 'x'
    return 'unknown'


def _read_reports(limit: int = 100):
    path = './data/reports.jsonl'
    items = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        return []
    items.reverse()
    return items[:limit]


@api.get('/', response_class=HTMLResponse)
def home():
    # Minimal static page that fetches /reports
    return HTMLResponse("""
<!doctype html>
<html>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Notebook</title>
<style>
body{margin:0;background:#0b0b0c;color:#f3f3f5;font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial}
.wrap{display:grid;grid-template-columns:320px 1fr;min-height:100vh}
.side{border-right:1px solid #1f1f22;padding:16px;overflow:auto}
.main{padding:24px}
.item{padding:8px 0;border-bottom:1px dashed #222}
.item a{color:#f3f3f5;text-decoration:none}
.item small{color:#9aa0a6;display:block}
.answer{background:#111214;border:1px solid #1f1f22;border-radius:10px;padding:16px}
.answer pre{background:#0f0f10;border:1px solid #26262a;border-radius:8px;padding:12px;overflow:auto}
.answer code{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace}
.answer h1,.answer h2,.answer h3{margin:14px 0 8px}
.answer ul{margin:8px 0 8px 18px}
.meta{color:#9aa0a6;margin:8px 0 16px}
</style>
</head>
<body>
<div class='wrap'>
  <aside class='side'>
    <h1>Reports</h1>
    <div id='list'>Loading…</div>
  </aside>
  <main class='main'>
    <div class='meta' id='meta'>No reports yet.</div>
    <div class='answer' id='answer'></div>
  </main>
</div>
<script>
const API = '/reports';
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function render(md){
  if(!md) return '';
  const blocks=[]; md=md.replace(/```([\s\S]*?)```/g,(_,c)=>{blocks.push(c);return '@@C'+(blocks.length-1)+'@@'});
  let h=esc(md);
  h=h.replace(/^###\s+(.*)$/gm,'<h3>$1</h3>').replace(/^##\s+(.*)$/gm,'<h2>$1</h2>').replace(/^#\s+(.*)$/gm,'<h1>$1</h1>');
  h=h.replace(/\[(.*?)\]\((https?:[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  h=h.replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>').replace(/\*(.*?)\*/g,'<em>$1</em>');
  h=h.replace(/^(?:- |\* )(.*)$/gm,'<li>$1</li>').replace(/(?:<li>.*<\/li>\n?)+/g,m=>'<ul>'+m+'</ul>');
  h=h.replace(/(^|\n)([^<\n][^\n]*)(?=\n|$)/g,(m,br,t)=>{if(/^\s*<\/?(h\d|ul|li|pre|code|blockquote)/i.test(t))return m;if(!t.trim())return m;return br+'<p>'+t+'</p>';});
  h=h.replace(/@@C(\d+)@@/g,(_,i)=>'<pre><code>'+esc(blocks[Number(i)])+'</code></pre>');
  return h;
}
function setAnswer(md){ document.getElementById('answer').innerHTML = render(md); }
async function load(){
  const list = document.getElementById('list');
  list.textContent = 'Starting to load...';
  console.log('Load function called');
  try{
    console.log('Fetching from:', API);
    const res = await fetch(API, {cache:'no-store', headers: {'Cache-Control': 'no-cache'}});
    console.log('Response status:', res.status);
    if(!res.ok){ list.textContent='Failed to load reports ('+res.status+').'; return; }
    const items = await res.json();
    console.log('Received items:', items.length, 'reports');
    if(!Array.isArray(items) || !items.length){ list.textContent='No reports yet.'; return; }
    list.innerHTML = items.map((it,i)=>{
      const q = it.query || ''; const preview = q.length>80? q.slice(0,80)+'…' : q; const url = it.post_url || '';
      return `<div class='item'><a href='#' data-idx='${i}'>${preview||'(no query)'}</a><small>${url}</small></div>`;
    }).join('');
    const latest = items[0];
    document.getElementById('meta').textContent = ((latest.source?('['+String(latest.source).toUpperCase()+'] '):'')) + (latest.post_url||'') + ' — ' + (latest.user_note||'');
    setAnswer(latest.compound_answer||'');
    list.addEventListener('click', (e)=>{
      const a = e.target.closest('a[data-idx]'); if(!a) return; e.preventDefault();
      const idx = parseInt(a.getAttribute('data-idx')); const it = items[idx];
      document.getElementById('meta').textContent = ((it.source?('['+String(it.source).toUpperCase()+'] '):'')) + (it.post_url||'') + ' — ' + (it.user_note||'');
      setAnswer(it.compound_answer||'');
    });
  }catch(err){ 
    console.error('Error loading reports:', err);
    list.textContent='Failed to load reports: ' + err.message; 
  }
}
if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', load); else load();
</script>
</body>
</html>
""")


@api.get('/reports', response_class=JSONResponse)
def api_reports():
    return _read_reports(200)


@api.post('/trigger')
async def trigger(req: Trigger):
    source = _detect_source(req.url)
    # Step 1: extract post content with Compound Mini
    post_text = _extract_post_with_kimi(req.url) or ''

    # Step 2: build query with Kimi from (post_text + user_note)
    query = _shape_query_with_kimi(post_text, req.note) or f"{req.note} (source: {source})"

    # Step 3: call Compound
    compound_answer = _compound_search(query)

    # Persist
    result: Dict[str, Any] = {
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'post_url': req.url,
        'user_note': req.note,
        'source': source,
        'post_text': post_text,
        'query': query,
        'compound_answer': compound_answer,
    }
    _append_jsonl('./data/reports.jsonl', result)
    return result


if __name__ == '__main__':
    import uvicorn
    async def _serve_both():
        config_x = uvicorn.Config('app2:api', host='127.0.0.1', port=8000, reload=False, log_level='info')
        config_li = uvicorn.Config('app2:api', host='127.0.0.1', port=8001, reload=False, log_level='info')
        server_x = uvicorn.Server(config_x)
        server_li = uvicorn.Server(config_li)
        await asyncio.gather(server_x.serve(), server_li.serve())
    asyncio.run(_serve_both())


