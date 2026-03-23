"""
analyze_fn_v2.py — Three-layer flow coverage analysis. Run directly:

  SRC_ROOT=/path/to/hyperswitch python3 analyze_fn_v2.py get_connector_with_networks --print-prompt
  SRC_ROOT=/path/to/hyperswitch python3 analyze_fn_v2.py get_connector_with_networks --model openai/kimi-latest --base-url https://...
"""
import argparse, json, os, re, sys, urllib.request
from neo4j import GraphDatabase

NEO4J_URI  = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "Hyperswitch@123")

# ── source reading ─────────────────────────────────────────────────────────────

def read_file(src_root, filepath):
    print(f"  reading file: {filepath!r} from src_root={bool(src_root)}", file=sys.stderr)
    if not src_root or not filepath: return []
    try:
        with open(os.path.join(src_root, filepath.lstrip('/')), errors='replace') as f:
            return f.readlines()
    except OSError: return []

# ── layer 1: call chain ────────────────────────────────────────────────────────
def get_fn_node(session, fn_name):
    rows = session.run("""
        MATCH (fn:CodeEntity) WHERE fn.label IN ['Function','Method']
          AND toLower(fn.name) CONTAINS toLower($name)
        RETURN fn.symbol AS symbol, fn.name AS name, fn.file AS file,
               fn.def_line AS def_line, coalesce(fn.cov_uncovered,0) AS cov_uncovered
        ORDER BY fn.cov_uncovered DESC LIMIT 10
    """, name=fn_name).data()
    if not rows: return None
    def _pref(r):
        f = r.get('file') or ''
        if 'bin/' in f or 'test' in f or 'openapi' in f: return 0
        if r.get('is_endpoint'): return 5
        if 'router/src/routes' in f: return 4
        if 'router/src/core' in f:   return 3
        if 'router/src' in f:        return 2
        return 1
    rows.sort(key=_pref, reverse=True)
    return rows[0]

def get_endpoints(session, fn_symbol, depth=6):
    rows = session.run(f"""
        MATCH (ep:CodeEntity {{is_endpoint:true}})
        MATCH (ep)-[rels:RELATION*1..{depth}]->(fn:CodeEntity {{symbol:$sym}})
        WHERE ALL(r IN rels WHERE r.type='CALLS')
        WITH DISTINCT ep, fn, shortestPath((ep)-[:RELATION*1..{depth}]->(fn)) AS sp
        WHERE ALL(r IN relationships(sp) WHERE r.type='CALLS')
        RETURN ep.symbol AS ep_symbol, ep.name AS ep_name,
               ep.http_method AS method, ep.http_path AS path,
               [n IN nodes(sp) | n.name] AS call_chain
        ORDER BY ep.http_path
    """, sym=fn_symbol).data()
    seen = {}
    for r in rows:
        k = r['ep_symbol']
        if k not in seen or len(r['call_chain']) < len(seen[k]['call_chain']): seen[k] = r
    return sorted(seen.values(), key=lambda r: (r['path'] or '', r['method'] or ''))

def get_hop_node(session, fn_name):
    rows = session.run("""
        MATCH (fn:CodeEntity) WHERE fn.name=$name AND fn.label IN ['Function','Method']
        RETURN fn.name AS name, fn.file AS file, fn.def_line AS def_line LIMIT 10
    """, name=fn_name).data()
    if not rows: return {'name': fn_name, 'file': None, 'def_line': None}
    # Preference order: routes/ > router/src/core > router/src > others; avoid bin/ openapi/ test
    def _pref(r):
        f = r.get('file') or ''
        if 'bin/' in f or 'test' in f or 'openapi' in f: return 0
        if 'router/src/routes' in f: return 4
        if 'router/src/core' in f:   return 3
        if 'router/src' in f:        return 2
        return 1
    rows.sort(key=_pref, reverse=True)
    return rows[0]

# ── layer 2: snippet extraction ────────────────────────────────────────────────
_IF = re.compile(r'^\s*(if\b|}\s*else\s+if\b)')
_IF_LET = re.compile(r'^\s*if\s+let\b')
_MATCH  = re.compile(r'^\s*match\b')
_CFG    = re.compile(r'#\[cfg\(')
_CLOSURE= re.compile(r'\.\s*(map|and_then|ok_or|unwrap_or_else|filter_map)\s*\(')

def find_fn_start(lines, def_line, fn_name):
    hint = max(0, (def_line or 1) - 1)
    def m(l): return f'fn {fn_name}(' in l or f'fn {fn_name}<' in l
    for i in range(max(0,hint-50), min(len(lines),hint+50)):
        if m(lines[i]): return i
    for i in range(len(lines)):
        if m(lines[i]): return i
    return hint

def find_call_site(lines, body_start, target):
    """
    Find line where target is called. Handles turbofish ::<T> syntax.
    Returns (call_line, actual_body_start) — if call was found via full-file
    scan, recomputes body_start by walking backward to the nearest fn def.
    """
    import re as _re
    _pat = _re.compile(r'(?<![\w])' + _re.escape(target) + r'(?:::<|\s*\()')
    fn_re = _re.compile(r'\s*(?:pub\s+)?(?:async\s+)?fn\s+')
    def is_call(l):
        return bool(_pat.search(l)) and ('fn ' + target) not in l

    # Pass 1: within expected body range
    for i in range(body_start, min(len(lines), body_start + 400)):
        if is_call(lines[i]):
            return i, body_start

    # Pass 2: full file — recompute body_start from actual call location
    for i in range(len(lines)):
        if is_call(lines[i]):
            # Walk backward to find nearest enclosing fn definition
            # Skip inner fn / closure fn — look for top-level or impl fn
            new_body = i
            for j in range(i, max(-1, i - 500), -1):
                if fn_re.match(lines[j]):
                    fn_line = lines[j].strip()
                    # Skip async closures and inner fns that are likely nested
                    # A real parent fn has lower indentation (starts near col 0)
                    indent = len(lines[j]) - len(lines[j].lstrip())
                    if indent <= 4:  # top-level or impl method (max 1 level of indent)
                        new_body = j
                        break
                    # Accept any fn if we haven't found a low-indent one yet
                    if new_body == i:
                        new_body = j
            return i, new_body

    return None, body_start


def classify(lines, call_line, body_start):
    """
    Walk FORWARD from body_start to call_line collecting all branch conditions.

    Forward scan is more reliable than backward because:
    - We follow code in the order it was written
    - No brace-depth tracking needed for string literals / macros
    - We track indent level instead — conditions that CONTAIN the call line
      are those whose indent is less than the call line's indent

    Returns dict with:
      type            : innermost condition type
      conditions      : list outermost→innermost [{type,text,line_no,snippet}]
      condition_text  : innermost text  (backward compat)
      condition_line  : innermost line  (backward compat)
      confidence      : 'high' | 'low' | 'none'
      snippet         : lines from outermost condition to call site
    """
    if call_line is None:
        return {'type':'not_found','condition_text':'','condition_line':None,
                'conditions':[],'confidence':'none','snippet':''}

    # Check for closure context immediately around call
    ctx = ''.join(lines[max(body_start, call_line-5):call_line+1])
    if _CLOSURE.search(ctx):
        cond = {'type':'closure','text':lines[call_line].strip(),
                'line_no':call_line+1,
                'snippet':''.join(lines[max(0,call_line-3):call_line+2])}
        return {'type':'closure','condition_text':lines[call_line].strip(),
                'condition_line':call_line+1,'conditions':[cond],
                'confidence':'low',
                'snippet':''.join(lines[max(0,call_line-3):call_line+2])}

    # Determine call site indent level — conditions must be less indented
    call_indent = len(lines[call_line]) - len(lines[call_line].lstrip())

    arm_re = re.compile(r'^\s+(\S[^=>{]*)\s*=>')

    def extract_cond_text(start):
        parts = []
        for j in range(start, min(len(lines), start + 6)):
            parts.append(lines[j].strip())
            if lines[j].rstrip().endswith('{'):
                break
        return ' '.join(parts)

    # Walk FORWARD from body_start to call_line.
    # Track which branch-opening lines are still "open" (not yet closed)
    # when we reach call_line. These are the enclosing conditions.
    #
    # Strategy: maintain a stack of open conditions.
    # When we see a branch opener (if/match/cfg) push it.
    # When we see a closing } at the same or lower indent as the opener, pop it.
    # Whatever is on the stack when we hit call_line = enclosing conditions.

    found_conditions = []
    stack = []   # list of {type, text, line_no, indent}

    _ARM_RE = re.compile(r'^\s*\w[^=]*=>\s*')   # match arm: Pattern =>

    for i in range(body_start, call_line + 1):  # include call_line for if-let-as-call
        line     = lines[i]
        stripped = line.lstrip()
        if not stripped:
            continue
        indent = len(line) - len(stripped)

        # Pop stack on }  BUT:
        # If next non-empty line is a match arm (Pattern =>), don't pop 'match' entries
        # because the match is still open across arm boundaries
        if stripped.startswith('}'):
            next_line = ''
            for j in range(i+1, min(len(lines), i+5)):
                if lines[j].strip():
                    next_line = lines[j]
                    break
            next_is_arm = bool(_ARM_RE.match(next_line))
            while stack and stack[-1]['indent'] >= indent:
                # Don't pop a match entry if next line is another arm
                if next_is_arm and stack[-1]['type'] == 'match':
                    break
                stack.pop()

        # Check if this line opens a new condition block
        cond_entry = None
        if _IF_LET.match(line):
            cond_entry = {'type':'if_let','text':extract_cond_text(i),
                          'line_no':i+1, 'indent':indent, 'snippet':''}
        elif _IF.match(line):
            cond_entry = {'type':'if',    'text':extract_cond_text(i),
                          'line_no':i+1, 'indent':indent, 'snippet':''}
        elif _MATCH.match(line):
            arm = next((arm_re.match(lines[j]).group(1).strip()
                        for j in range(i+1, call_line+1) if arm_re.match(lines[j])), '')
            cond_entry = {'type':'match',
                          'text':stripped.rstrip()+(' => '+arm if arm else ''),
                          'line_no':i+1, 'indent':indent, 'snippet':''}

        if cond_entry:
            # Pop any stack entries at the same or deeper indent
            # (sibling/nested branches that were already closed)
            while stack and stack[-1]['indent'] >= indent:
                stack.pop()
            stack.append(cond_entry)

    # Whatever is on the stack at call_line = enclosing conditions
    found_conditions = list(stack)

    if not found_conditions:
        ctx_start = max(body_start, call_line - 5)
        snippet   = ''.join(lines[ctx_start: min(len(lines), call_line + 5)])
        return {'type':'unconditional','condition_text':'unconditional',
                'condition_line':None,'conditions':[],
                'confidence':'high','snippet':snippet}

    # Each condition snippet = just its own lines up to the opening {
    for c in found_conditions:
        start = c['line_no'] - 1   # 0-indexed
        cond_lines = []
        for j in range(start, min(len(lines), start + 8)):
            cond_lines.append(lines[j].rstrip())
            if lines[j].rstrip().endswith('{'):
                break
        c['snippet'] = '\n'.join(cond_lines)

    # Full snippet from outermost condition to call site (for context)
    outermost_start = found_conditions[0]['line_no'] - 1
    full_snippet    = ''.join(lines[outermost_start: min(len(lines), call_line + 2)])

    # Innermost = last in list (highest line_no < call_line)
    innermost = found_conditions[-1]

    return {
        'type':           innermost['type'],
        'condition_text': innermost['text'],
        'condition_line': innermost['line_no'],
        'conditions':     found_conditions,   # outermost → innermost
        'confidence':     'high',
        'snippet':        full_snippet,
    }


def mini_llm(fn_source, caller, target, model, base_url, api_key):
    if not model or not base_url: return ''
    prompt = (f"Rust source of `{caller}`. What condition gates the call to `{target}`?\n"
              f"ONE sentence or 'unconditional'. No code blocks.\n\nSOURCE:\n{fn_source[:3000]}")
    key = api_key or os.environ.get('LITELLM_API_KEY') or os.environ.get('OPENAI_API_KEY') or 'no-key'
    body = json.dumps({'model':model.split('/',1)[-1],'messages':[{'role':'user','content':prompt}],'max_tokens':150}).encode()
    try:
        req = urllib.request.Request(base_url.rstrip('/')+'/chat/completions', data=body,
            headers={'Content-Type':'application/json','Authorization':f'Bearer {key}'}, method='POST')
        with urllib.request.urlopen(req, timeout=30) as r: data = json.loads(r.read())
        msg = data['choices'][0]['message']
        return (msg.get('content') or msg.get('reasoning_content') or '').strip()
    except Exception as e: return f'(mini LLM failed: {e})'

def get_call_site_line(session, caller_name, callee_name):
    """
    Get the exact call site line from the CALLS edge stored in Neo4j.
    Returns 0-indexed line or None if not found / not yet stored.
    """
    rows = session.run("""
        MATCH (caller:CodeEntity {name: $caller})-[e:RELATION {type:'CALLS'}]->
              (callee:CodeEntity {name: $callee})
        WHERE e.line IS NOT NULL
          AND caller.file CONTAINS 'router/src'
        RETURN e.line AS line
        ORDER BY e.line
        LIMIT 1
    """, caller=caller_name, callee=callee_name).data()
    if rows and rows[0].get('line') is not None:
        return rows[0]['line']  # already 0-indexed (SCIP occ.range[0])
    return None


def extract_chain(driver, call_chain, src_root, model, base_url, api_key):
    hops = []
    with driver.session() as s:
        for name in call_chain: hops.append(get_hop_node(s, name))
    n = len(hops)
    result = []
    for i, hop in enumerate(hops):
        is_target = (i == n-1)
        is_direct = (i == n-2)
        print(f"    [{i+1}/{n}] {hop['name']}", file=sys.stderr)
        if is_target:
            lines = read_file(src_root, hop.get('file'))
            start = find_fn_start(lines, hop.get('def_line'), hop['name']) if lines else 0
            src   = ''.join(lines[start:min(len(lines),start+100)]) if lines else ''
            hop = dict(hop); hop['source'] = src
            hop['extraction'] = {'type':'target','condition_text':'','confidence':'n/a','snippet':src}
            result.append(hop); print(f"      target, {len(src)} chars", file=sys.stderr); continue
        next_fn = hops[i+1]['name']
        lines = read_file(src_root, hop.get('file'))
        if not lines:
            hop = dict(hop); hop['extraction'] = {'type':'not_found','condition_text':'','confidence':'none','snippet':''}
            result.append(hop); print(f"      file not found", file=sys.stderr); continue
        body_start = find_fn_start(lines, hop.get('def_line'), hop['name'])
        # Try graph first for exact call site line
        with driver.session() as s:
            graph_call_line = get_call_site_line(s, hop['name'], next_fn)
        if graph_call_line is not None:
            import re as _re2
            _fn_re2 = _re2.compile(r'\s*(?:pub\s+)?(?:async\s+)?fn\s+')
            for j in range(graph_call_line, max(-1, graph_call_line - 500), -1):
                if _fn_re2.match(lines[j]):
                    body_start = j
                    break
            call_line = graph_call_line
            print(f"      body={body_start} call={call_line} (graph)", file=sys.stderr)
        else:
            call_line, body_start = find_call_site(lines, body_start, next_fn)
            if call_line is not None and call_line <= body_start:
                body_start = max(0, call_line - 20)
            print(f"      body={body_start} call={call_line} (text)", file=sys.stderr)
        ext = classify(lines, call_line, body_start)
        is_entry_point = (i == 0)
        needs_llm = (ext['type'] in ('not_found','closure')
                     and model and base_url
                     and not is_entry_point)  # entry points call next fn unconditionally
        if needs_llm:
            fn_src = ''.join(lines[body_start:min(len(lines),body_start+300)])
            print(f"      [mini-LLM] {hop['name']} → {next_fn}", file=sys.stderr)
            ans = mini_llm(fn_src, hop['name'], next_fn, model, base_url, api_key)
            # Only use LLM answer if it succeeded and found a real condition
            if ans and 'unconditional' not in ans.lower() and 'failed' not in ans.lower():
                ext = {'type':'llm_inferred','condition_text':ans,'confidence':'inferred','snippet':fn_src[:400]}
            else:
                ext['type'] = 'unconditional'
                ext['confidence'] = 'high'
                ext['condition_text'] = 'unconditional'
        elif is_entry_point and ext['type'] in ('not_found', 'unconditional_or_deep','unconditional'):
            ext['type'] = 'unconditional'
            ext['confidence'] = 'high'
            ext['condition_text'] = 'unconditional (entry point handler)'  
        hop = dict(hop); hop['extraction'] = ext
        if is_direct:  # full source for direct caller — use graph body_start
            start = body_start  # already resolved from graph or text search
            depth2, end = 0, start
            for j in range(start, min(len(lines), start + 300)):
                depth2 += lines[j].count('{') - lines[j].count('}')
                end = j
                if depth2 <= 0 and j > start:
                    break
            hop['full_source'] = ''.join(lines[start:end+1])
        result.append(hop)
        print(f"      → {ext['type']} conf={ext['confidence']}", file=sys.stderr)
    return result

# ── layer 3: rich context ──────────────────────────────────────────────────────
_CPATHS = ('/payments/','/refunds/','/payouts/','/mandates/')
_TRAITS = {'confirm':'PaymentAuthorize','capture':'PaymentCapture','cancel':'PaymentVoid',
           'refund':'Refund','sync':'PaymentSync','session':'PaymentSession',
           'incremental_authorization':'IncrementalAuthorization','payout':'Payouts'}

def get_connectors(src_root, path):
    print(f"  connectors: path={path!r} src_root={bool(src_root)}", file=sys.stderr)
    if not src_root or not any(f in (path or '') for f in _CPATHS):
        print("  → skipping", file=sys.stderr); return []
    trait = next((t for frag,t in _TRAITS.items() if frag in (path or '')), 'PaymentAuthorize')
    cdir  = os.path.join(src_root, 'crates','hyperswitch_connectors','src','connectors')
    try: names = [d for d in os.listdir(cdir) if os.path.isfile(os.path.join(cdir,d+'.rs'))]
    except OSError as e: print(f"  → dir error: {e}", file=sys.stderr); return []
    print(f"  → {len(names)} connector files, checking {trait}", file=sys.stderr)
    result = []
    for name in names:
        try:
            c = open(os.path.join(cdir, name+'.rs'), errors='replace').read()
            if f'api::{trait} for' in c or f'ConnectorIntegration<{trait},' in c: result.append(name)
        except OSError: pass
    print(f"  → {len(result)} match", file=sys.stderr)
    return sorted(result)

def get_struct_defs(driver, endpoints, src_root):
    ep_syms = [ep['ep_symbol'] for ep in endpoints[:3] if ep.get('ep_symbol')]
    print(f"  structs: {len(ep_syms)} ep_symbols", file=sys.stderr)
    if not ep_syms or not src_root: return {}
    with driver.session() as s:
        rows = s.run("""
            UNWIND $syms AS sym
            MATCH (ep:CodeEntity {symbol:sym})-[:RELATION {type:'USES_TYPE'}]->(st:CodeEntity)
            WHERE st.label IN ['Struct','Enum'] AND st.file CONTAINS 'api_models'
              AND st.name CONTAINS 'Request'
            RETURN DISTINCT st.name AS name, st.file AS file, st.def_line AS def_line
            ORDER BY st.name LIMIT 6
        """, syms=ep_syms).data()
        if not rows:
            rows = s.run("""
                MATCH (st:CodeEntity) WHERE st.label IN ['Struct','Enum']
                  AND st.file CONTAINS 'api_models'
                  AND st.name IN ['PaymentsRequest','PaymentsConfirmRequest',
                                   'PaymentsCaptureRequest','PaymentsCancelRequest','RefundsRequest']
                RETURN DISTINCT st.name AS name, st.file AS file, st.def_line AS def_line
                ORDER BY st.name LIMIT 5
            """).data()
    print(f"  → structs found: {[r['name'] for r in rows]}", file=sys.stderr)
    result = {}
    for r in rows:
        lines = read_file(src_root, r['file'])
        if not lines: continue
        start = find_fn_start(lines, r.get('def_line'), r['name'])
        brace,end = 0, start
        for i in range(start, min(len(lines),start+80)):
            brace += lines[i].count('{') - lines[i].count('}'); end = i
            if brace <= 0 and i > start: break
        result[r['name']] = ''.join(lines[start:end+1])
    return result

def get_var_types(driver, chain_snippets, src_root):
    var_re = re.compile(r'\b([a-z][a-z_]+)\.(is_[a-z_]+|[a-z_]+_enabled)\b')
    variables = {}
    for hop in chain_snippets:
        cond = hop.get('extraction',{}).get('condition_text','')
        for m in var_re.finditer(cond):
            key = f'{m.group(1)}.{m.group(2)}'
            if key not in variables: variables[key] = {'field':m.group(2),'condition':cond}
    result = []
    for key, info in list(variables.items())[:8]:
        with driver.session() as s:
            rows = s.run("MATCH (f:CodeEntity {kind:'field'}) WHERE f.name=$f AND f.file IS NOT NULL "
                         "AND NOT f.file CONTAINS 'test' RETURN f.name AS name, f.file AS file, "
                         "f.def_line AS def_line LIMIT 3", f=info['field']).data()
        for r in rows:
            lines = read_file(src_root, r['file']) if src_root else []
            lt = lines[r['def_line']-1].strip() if lines and r.get('def_line') and r['def_line']-1 < len(lines) else ''
            result.append({'key':key,'field':info['field'],'file':r['file'],
                           'line_no':r.get('def_line'),'source':lt,'condition':info['condition']}); break
    return result

# ── prompt builder ─────────────────────────────────────────────────────────────
def build_prompt(fn_node, endpoints, chain_snippets, connectors, struct_defs, var_types):
    P = []
    def p(*a): P.append(' '.join(str(x) for x in a))
    p("You are an expert in the Hyperswitch payments API codebase."); p("")
    p("="*60); p("TARGET FUNCTION"); p("="*60)
    p(f"Name : {fn_node['name']}"); p(f"File : {fn_node.get('file')}:{fn_node.get('def_line')}"); p("")
    p("="*60); p(f"ENTRY POINTS ({len(endpoints)} endpoints reach this function)"); p("="*60)
    for ep in endpoints: p(f"  {ep['method']:7s} {ep['path']:<55} ({ep.get('ep_name','')})")
    p("")
    p("="*60); p("CALL CHAIN CONDITIONS"); p("Each hop: what gates the call to the next function."); p("="*60)
    n = len(chain_snippets)
    for i, hop in enumerate(chain_snippets):
        is_t = (i==n-1); is_d = (i==n-2)
        tag = "  [TARGET]" if is_t else ("  [DIRECT CALLER]" if is_d else "")
        p(f"\n[{i+1}/{n}] {hop['name']}{tag}")
        p(f"  File : {hop.get('file')}:{hop.get('def_line')}")
        if is_t or is_d:
            src = hop.get('full_source') or hop.get('source') or ''
            if src:
                p("  Full source:")
                for l in src.splitlines()[:120]: p("    "+l)
            else: p("  Full source: (not available — SRC_ROOT required)")
        else:
            ext = hop.get('extraction',{})
            if ext.get('condition_text'):
                p(f"  Condition ({ext['type']}, confidence={ext['confidence']}):")
                p(f"    {ext['condition_text'][:200]}")
            if ext.get('snippet'):
                p("  Code context:")
                for l in ext['snippet'].splitlines()[:8]: p("    "+l)
            if not ext.get('condition_text'): p("  Condition: unconditional (could not be extracted)")
    p("")
    if var_types:
        p("="*60); p("CONDITION VARIABLE TYPES"); p("="*60)
        for v in var_types:
            p(f"  {v['key']}"); p(f"    File  : {v['file']} L{v['line_no']}")
            p(f"    Source: {v['source']}"); p(f"    Used in: {v['condition'][:120]}")
        p("")
    if struct_defs:
        p("="*60); p("REQUEST STRUCT DEFINITIONS"); p("="*60)
        for name, src in list(struct_defs.items())[:4]:
            p(f"\n{name}:")
            for l in src.splitlines()[:30]: p("  "+l)
        p("")
    if connectors:
        p("="*60); p(f"CONNECTORS ({len(connectors)} implement this flow)"); p("="*60)
        for i in range(0,len(connectors),8): p("  "+"  ".join(connectors[i:i+8]))
        p("")
    p("="*60); p("YOUR TASK"); p("="*60)
    p("Identify every distinct execution path through the TARGET function. Each path = a FLOW."); p("")
    p("For EACH flow:"); p("")
    p("FLOW N: <description>"); p("")
    p("  1. IMPACT — which entry points (METHOD + PATH) exercise this path"); p("")
    p("  2. CONNECTORS — which from the list are relevant and why others excluded"); p("")
    p("  3. SETUP — prerequisites before calling the endpoint")
    p("       What it is + why needed + where (request body/business profile/routing config)")
    p("       How to set: API: METHOD PATH Body:{} OR Manual: config/DB"); p("")
    p("  4. TRIGGER — exact endpoint call")
    p("       Endpoint: METHOD /path"); p("       Body: {fields from request structs}")
    p("       Real values only — no placeholders"); p("")
    p("  5. DISABLE TEST — toggle=false → same trigger → expected fallback behaviour"); p("")
    p("Rules:")
    p("  - Read conditions literally. Do not guess.")
    p("  - Trigger fields come from request struct definitions, not memory.")
    p("  - business_profile fields = setup API call, not request body field.")
    p("  - If unreachable via HTTP: write Rust unit test.")
    p("  - Order flows by coverage impact (most uncovered lines first).")
    return "\n".join(P)

# ── llm call ───────────────────────────────────────────────────────────────────
def call_llm(prompt, model, base_url, api_key, max_tokens=8000):
    key  = api_key or os.environ.get('LITELLM_API_KEY') or os.environ.get('OPENAI_API_KEY') or 'no-key'
    body = json.dumps({'model':model.split('/',1)[-1],'messages':[{'role':'user','content':prompt}],'max_tokens':max_tokens}).encode()
    try:
        req = urllib.request.Request(base_url.rstrip('/')+'/chat/completions', data=body,
            headers={'Content-Type':'application/json','Authorization':f'Bearer {key}'}, method='POST')
        with urllib.request.urlopen(req, timeout=180) as r: data = json.loads(r.read())
        msg = data['choices'][0]['message']
        print(msg.get('content') or msg.get('reasoning_content') or '')
    except Exception as e: print(f"LLM error: {e}")

# ── main ───────────────────────────────────────────────────────────────────────
def _describe_flow(chain_snippets, model, base_url, api_key):
    """
    Derive a human-readable description of the flow from extracted conditions.
    Mechanical if all conditions are high-confidence, mini LLM otherwise.
    """
    # Collect all high-confidence non-trivial conditions
    conds = []
    for hop in chain_snippets[:-1]:  # exclude target itself
        ext = hop.get('extraction', {})
        text = ext.get('condition_text', '')
        if (ext.get('confidence') == 'high'
                and text
                and 'unconditional' not in text.lower()):
            conds.append(f"{hop['name']}: {text[:80]}")

    if conds:
        # Mechanical: concatenate the key conditions into a readable label
        # Extract the most specific condition (N-1 hop = direct caller)
        direct = chain_snippets[-2] if len(chain_snippets) >= 2 else None
        if direct:
            ext = direct.get('extraction', {})
            cond = ext.get('condition_text', '')
            if cond:
                # Trim to essentials
                cond = cond.replace('if ', '').replace('if let ', '').strip()
                if len(cond) > 100:
                    cond = cond[:100] + '...'
                return cond
        return ' + '.join(conds[:2])

    # LLM fallback for low-confidence extractions
    if not model or not base_url:
        return 'flow (condition not extracted mechanically)'

    snippets_text = '\n'.join(
        f"{h['name']}: {h.get('extraction',{}).get('snippet','')[:200]}"
        for h in chain_snippets[:-1]
    )
    return mini_llm(
        snippets_text,
        'call_chain',
        chain_snippets[-1]['name'],
        model, base_url, api_key
    ) or 'flow (LLM extraction failed)'



# ══════════════════════════════════════════════════════════════════════
# PREREQUISITE EXTRACTION (mechanical, 3-rule)
# ══════════════════════════════════════════════════════════════════════

_FIELD_RE = re.compile(
    r'\b(?:business_profile|profile|merchant_account|connector_config)'
    r'\.([a-z][a-z_0-9]+)'
)

def _extract_condition_fields(chain_nodes):
    """Extract struct.field references from all condition texts in the chain."""
    fields = {}
    for node in chain_nodes:
        if node.get('role') == 'target':
            continue
        cond = node.get('condition', {})
        for c in cond.get('conditions', []):
            for m in _FIELD_RE.finditer(c.get('text', '')):
                fields[m.group(1)] = c.get('text', '')
        for m in _FIELD_RE.finditer(cond.get('text', '')):
            fields[m.group(1)] = cond.get('text', '')
    return fields


def _rule1_toggle_endpoint(driver, field_name):
    """Rule 1: dedicated toggle endpoint whose path contains the field fragment."""
    fragment = field_name
    for prefix in ('is_', 'enable_', 'enabled_'):
        if fragment.startswith(prefix):
            fragment = fragment[len(prefix):]
    for suffix in ('_enabled', '_enable'):
        if fragment.endswith(suffix):
            fragment = fragment[:-len(suffix)]
    fragment_dash = fragment.replace('_', '-')

    with driver.session() as s:
        rows = s.run("""
            MATCH (ep:CodeEntity {is_endpoint:true})
            WHERE ep.http_method IN ['POST','PUT','PATCH']
              AND (ep.http_path CONTAINS $frag OR ep.http_path CONTAINS $frag_dash)
              AND ep.http_path CONTAINS 'business_profile'
            RETURN ep.http_method AS method, ep.http_path AS path
            ORDER BY ep.http_path LIMIT 1
        """, frag=fragment, frag_dash=fragment_dash).data()
    if rows:
        return {'method': rows[0]['method'], 'path': rows[0]['path'],
                'payload': {'enabled': True}}
    return None


def _rule2_profile_update_field(driver, field_name, src_root):
    """Rule 2: field in a Profile* update struct in api_models/admin.rs."""
    if not src_root:
        return None
    fpath = os.path.join(src_root, 'crates', 'api_models', 'src', 'admin.rs')
    if not os.path.exists(fpath):
        return None
    import re as _re
    struct_re = _re.compile(r'\s*(?:pub\s+)?struct\s+(\w+)')
    current_struct = None
    try:
        for line in open(fpath, errors='replace'):
            m = struct_re.match(line)
            if m:
                current_struct = m.group(1)
            if field_name in line and current_struct and \
               any(k in current_struct for k in ['Update', 'Create', 'Request']):
                with driver.session() as s:
                    rows = s.run("""
                        MATCH (st:CodeEntity {name:$s})
                        MATCH (ep:CodeEntity {is_endpoint:true})-[:RELATION*1..4]->(:CodeEntity)
                              -[:RELATION]->(st)
                        WHERE ep.http_method IN ['POST','PUT','PATCH']
                          AND (ep.http_path CONTAINS 'profile'
                               OR ep.http_path CONTAINS 'account'
                               OR ep.http_path CONTAINS 'merchant')
                        RETURN DISTINCT ep.http_method AS method, ep.http_path AS path
                        ORDER BY size(ep.http_path) DESC
                        LIMIT 3
                    """, s=current_struct).data()
                    # Prefer the most specific profile path
                    rows = [r for r in rows if 'profile' in r['path']] or rows
                if rows:
                    return {'method': rows[0]['method'], 'path': rows[0]['path'],
                            'struct': current_struct, 'payload': {field_name: True}}
    except OSError:
        pass
    return None


def _rule3_setter_fn_endpoint(driver, field_name):
    """Rule 3: endpoint that calls a setter fn (admin.rs) referencing the field."""
    with driver.session() as s:
        rows = s.run("""
            MATCH (setter:CodeEntity)-[:RELATION]->(f:CodeEntity {name:$field})
            WHERE setter.label IN ['Function','Method']
              AND setter.file CONTAINS 'admin.rs'
            MATCH (ep:CodeEntity {is_endpoint:true})-[:RELATION*1..5]->(setter)
            WHERE ep.http_method IN ['POST','PUT','PATCH']
            RETURN DISTINCT ep.http_method AS method, ep.http_path AS path
            ORDER BY ep.http_path LIMIT 5
        """, field=field_name).data()
    # prefer PUT with profile in path (profile update), else shortest profile path
    profile_rows = [r for r in rows if 'profile' in r['path']]
    put_rows = [r for r in profile_rows if r['method'] == 'PUT']
    best = (put_rows or profile_rows or rows or [None])[0]
    if best:
        return {'method': best['method'], 'path': best['path'],
                'payload': {field_name: True},
                'note': f"field '{field_name}' not found in api_models — verify request field name"}
    return None


def extract_prerequisites(driver, chain_nodes, src_root):
    """Mechanically derive setup prerequisites from chain conditions (3 rules)."""
    fields = _extract_condition_fields(chain_nodes)
    if not fields:
        return []

    prerequisites = []
    seen_eps = set()

    for field_name, condition_text in fields.items():
        r1 = _rule1_toggle_endpoint(driver, field_name)
        if r1:
            ep_key = f"{r1['method']} {r1['path']}"
            if ep_key not in seen_eps:
                seen_eps.add(ep_key)
                prerequisites.append({
                    'field': field_name, 'condition': condition_text[:100],
                    'config_endpoint': ep_key, 'config_value': r1['payload'],
                    'confidence': 'high', 'rule': 'toggle_endpoint',
                })
            continue

        r2 = _rule2_profile_update_field(driver, field_name, src_root)
        if r2:
            ep_key = f"{r2['method']} {r2['path']}"
            if ep_key not in seen_eps:
                seen_eps.add(ep_key)
                prerequisites.append({
                    'field': field_name, 'condition': condition_text[:100],
                    'config_endpoint': ep_key, 'config_value': r2['payload'],
                    'confidence': 'high', 'rule': 'profile_update_struct',
                })
            continue

        r3 = _rule3_setter_fn_endpoint(driver, field_name)
        if r3:
            ep_key = f"{r3['method']} {r3['path']}"
            if ep_key not in seen_eps:
                seen_eps.add(ep_key)
                prerequisites.append({
                    'field': field_name, 'condition': condition_text[:100],
                    'config_endpoint': ep_key, 'config_value': r3['payload'],
                    'confidence': 'low', 'rule': 'setter_fn_trace',
                    'note': r3.get('note', ''),
                })

    return prerequisites

def export_fn_json(fn_name, output_path, depth=6,
                   model=None, base_url=None, api_key=None):
    """
    Export structured JSON for a function:
      - all endpoints reaching it
      - flow graph per unique call chain (with branching code snippets)
      - exact connector list per flow

    Uses LLM only when mechanical condition extraction fails.
    """
    src_root = os.environ.get('SRC_ROOT', '')
    driver   = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)

    # ── Resolve function ──────────────────────────────────────────────
    with driver.session() as s:
        fn_node = get_fn_node(s, fn_name)
    if not fn_node:
        print(f"No function matching '{fn_name}'"); return

    print(f"  function: {fn_node['name']}  {fn_node.get('file')} L{fn_node.get('def_line')}", file=sys.stderr)

    # ── Endpoints ─────────────────────────────────────────────────────
    print("  loading endpoints ...", file=sys.stderr)
    with driver.session() as s:
        endpoints = get_endpoints(s, fn_node['symbol'], depth)

    if not endpoints:
        with driver.session() as s:
            self_ep = s.run("""
                MATCH (ep:CodeEntity {name:$name, is_endpoint:true})
                RETURN ep.http_method AS method, ep.http_path AS path,
                       ep.name AS ep_name, [ep.name] AS call_chain,
                       ep.symbol AS ep_symbol
                LIMIT 1
            """, name=fn_node['name']).data()
        if self_ep:
            ep = self_ep[0]
            endpoints = [{
                'ep_symbol': ep['ep_symbol'], 'ep_name': ep['ep_name'],
                'method': ep['method'], 'path': ep['path'],
                'call_chain': ep['call_chain'],
            }]
            print(f"  self-endpoint: {ep['method']} {ep['path']}", file=sys.stderr)
        else:
            print(f"  No endpoints found within depth {depth}.")
            driver.close(); return


    endpoints_out = [
        {
            'method':  ep['method'],
            'path':    ep['path'],
            'handler': ep.get('ep_name', ''),
            'chain':   ep['call_chain'],
        }
        for ep in endpoints
    ]

    # ── Unique chains → flows ─────────────────────────────────────────
    print("  computing unique chains ...", file=sys.stderr)
    # Deduplicate by intermediate chain (everything after the entry point handler).
    # Different handlers (payments_confirm, payments_create...) that share the
    # same downstream path (payments_core → ... → target) produce ONE flow.
    seen_chains  = {}   # intermediate_sig → representative endpoint
    for ep in endpoints:
        # Use chain[1:] so we group by shared intermediate path, not entry handler
        intermediate_sig = tuple(ep['call_chain'][1:])
        if intermediate_sig not in seen_chains:
            # Among endpoints with same intermediate chain, pick the most specific
            seen_chains[intermediate_sig] = ep
        else:
            # Keep the more specific endpoint (prefer confirm > capture > create > get)
            def _score(e):
                p = e.get('path') or ''
                m = (e.get('method') or '').upper()
                priority = {'confirm':10,'capture':9,'cancel':8,'refund':7,'sync':6}
                return (max((v for k,v in priority.items() if k in p), default=0),
                        2 if m=='POST' else 0)
            if _score(ep) > _score(seen_chains[intermediate_sig]):
                seen_chains[intermediate_sig] = ep

    flows_out = []
    for flow_idx, (sig, ep) in enumerate(seen_chains.items(), 1):  # sig = intermediate chain
        chain = ep['call_chain']
        print(f"  flow {flow_idx}: {' -> '.join(chain)}", file=sys.stderr)

        # Extract snippets for this chain
        snippets = extract_chain(
            driver, chain, src_root,
            model, base_url, api_key
        )

        # Build chain node list
        chain_nodes = []
        for i, hop in enumerate(snippets):
            is_target = (i == len(snippets) - 1)
            ext       = hop.get('extraction', {})

            node = {
                'function':   hop['name'],
                'file':       hop.get('file'),
                'def_line':   hop.get('def_line'),
                'role':       'target' if is_target else (
                              'direct_caller' if i == len(snippets) - 2 else 'intermediate'),
            }

            if is_target:
                node['source'] = hop.get('source', '')
            else:
                node['condition'] = {
                    'type':           ext.get('type', 'unknown'),
                    'text':           ext.get('condition_text', ''),
                    'condition_line': ext.get('condition_line'),
                    'conditions':     ext.get('conditions', []),
                    'confidence':     ext.get('confidence', 'none'),
                    'snippet':        ext.get('snippet', ''),
                }
                if i == len(snippets) - 2:
                    node['full_source'] = hop.get('full_source', '')

            chain_nodes.append(node)

        # Extract prerequisites mechanically from conditions
        prerequisites = extract_prerequisites(driver, chain_nodes, src_root)

        # Describe the flow
        description = _describe_flow(snippets, model, base_url, api_key)

        # Connectors for this flow's endpoint path
        connectors = get_connectors(src_root, ep['path'])

        # Which endpoints share this intermediate chain
        sharing_endpoints = [
            {'method': e['method'], 'path': e['path'], 'handler': e.get('ep_name', '')}
            for e in endpoints
            if tuple(e['call_chain'][1:]) == sig
        ]

        flows_out.append({
            'flow_id':           flow_idx,
            'description':       description,
            'endpoints':         sharing_endpoints,
            'prerequisites':     prerequisites,
            'chain':             chain_nodes,
            'connectors':        connectors,
            'connector_count':   len(connectors),
            'conditions_high':   sum(
                1 for n in chain_nodes
                if n.get('condition', {}).get('confidence') == 'high'
            ),
            'conditions_inferred': sum(
                1 for n in chain_nodes
                if n.get('condition', {}).get('confidence') == 'inferred'
            ),
            'conditions_missing': sum(
                1 for n in chain_nodes
                if n.get('condition', {}).get('confidence') in ('none', 'low', None)
                   and not n.get('role') == 'target'
            ),
        })

    # ── Assemble output ───────────────────────────────────────────────
    output = {
        'function':      fn_node['name'],
        'file':          fn_node.get('file'),
        'def_line':      fn_node.get('def_line'),
        'endpoint_count': len(endpoints),
        'flow_count':     len(flows_out),
        'endpoints':     endpoints_out,
        'flows':         flows_out,
    }

    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"\n  Exported {len(flows_out)} flows, {len(endpoints)} endpoints → {output_path}")
    driver.close()
    return output

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fn_name"); ap.add_argument("--depth",type=int,default=6)
    ap.add_argument("--model",default=None); ap.add_argument("--base-url",default=None)
    ap.add_argument("--api-key",default=None); ap.add_argument("--print-prompt",action="store_true")
    ap.add_argument("--export-json", default=None, metavar="FILE",
                    help="Export endpoints, flow graph, and connectors to JSON file")
    args = ap.parse_args()
    src_root = os.environ.get('SRC_ROOT','')
    driver   = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)

    with driver.session() as s: fn_node = get_fn_node(s, args.fn_name)
    if not fn_node: print(f"No function matching '{args.fn_name}'"); return
    print(f"\n  FUNCTION : {fn_node['name']}\n  FILE     : {fn_node.get('file')} L{fn_node.get('def_line')}")

    print("\n  Layer 1 — call chain ...", file=sys.stderr)
    with driver.session() as s: endpoints = get_endpoints(s, fn_node['symbol'], args.depth)
    if not endpoints:
        # Check if the function itself is an endpoint handler
        with driver.session() as s:
            self_ep = s.run("""
                MATCH (ep:CodeEntity {name:$name, is_endpoint:true})
                RETURN ep.http_method AS method, ep.http_path AS path,
                       ep.name AS ep_name, [ep.name] AS call_chain,
                       ep.symbol AS ep_symbol
                LIMIT 1
            """, name=fn_node['name']).data()
        if self_ep:
            ep = self_ep[0]
            endpoints = [{
                'ep_symbol': ep['ep_symbol'], 'ep_name': ep['ep_name'],
                'method': ep['method'], 'path': ep['path'],
                'call_chain': ep['call_chain'],
            }]
            print(f"  Function is itself an endpoint handler: {ep['method']} {ep['path']}", file=sys.stderr)
        else:
            print(f"  No endpoints found within depth {args.depth}."); return

    # Print all endpoints to stdout
    col_m = max(len(ep['method'] or '') for ep in endpoints)
    col_p = max(len(ep['path']   or '') for ep in endpoints)
    print(f"\n  ENDPOINTS ({len(endpoints)} reach this function)")
    print(f"  {'─'*60}")
    for ep in endpoints:
        print(f"  {(ep['method'] or ''):<{col_m}}  {(ep['path'] or ''):<{col_p}}  ({ep.get('ep_name','')})")

    print(f"  {len(endpoints)} endpoint(s)", file=sys.stderr)

    # Pick the most relevant entry point — prefer POST with {payment_id},
    # then POST, then GET. Within same method, prefer longer/more specific paths.
    def _ep_score(ep):
        method = (ep.get('method') or '').upper()
        path   = ep.get('path') or ''
        # Highest priority: confirm > capture > cancel > refund > create > others
        priority = {'confirm': 10, 'capture': 9, 'cancel': 8, 'refund': 7,
                    'sync': 6, 'session': 5}
        p = max((v for k, v in priority.items() if k in path), default=0)
        method_score = 2 if method == 'POST' else (1 if method == 'PUT' else 0)
        has_id = 1 if '{payment_id}' in path or '{id}' in path else 0
        return (p, method_score, has_id, len(path))

    seen = set()
    primary = sorted(endpoints, key=_ep_score, reverse=True)[0]
    for ep in endpoints:
        sig = tuple(ep['call_chain'])
        seen.add(sig)

    print(f"\n  PRIMARY CHAIN: {' -> '.join(primary['call_chain'])}")
    print(f"  {'─'*60}")
    print(f"  Chain: {' -> '.join(primary['call_chain'])}", file=sys.stderr)
    print("\n  Layer 2 — extracting snippets ...", file=sys.stderr)
    chain_snippets = extract_chain(driver, primary['call_chain'], src_root, args.model, args.base_url, args.api_key)

    print("\n  Layer 3 — rich context ...", file=sys.stderr)
    connectors  = get_connectors(src_root, primary['path'])
    struct_defs = get_struct_defs(driver, [primary], src_root)
    var_types   = get_var_types(driver, chain_snippets, src_root)

    print("\n  Building prompt ...", file=sys.stderr)
    prompt = build_prompt(fn_node, endpoints, chain_snippets, connectors, struct_defs, var_types)

    if args.export_json:
        driver.close()
        export_fn_json(
            fn_name     = args.fn_name,
            output_path = args.export_json,
            depth       = args.depth,
            model       = args.model,
            base_url    = args.base_url,
            api_key     = args.api_key,
        )
        return

    if args.print_prompt: print(prompt); return
    if not args.model: print("\nAdd --model and --base-url to send to LLM.\nUse --print-prompt to inspect."); return
    print("\n  Calling LLM ...\n"); call_llm(prompt, args.model, args.base_url, args.api_key)
    driver.close()










if __name__ == '__main__': main()