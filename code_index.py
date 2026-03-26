import re
import os
from typing import Dict, List, Optional
from collections import defaultdict

try:
    from scip import scip_pb2
except ImportError:
    import scip_pb2  # generated via: python3 -m grpc_tools.protoc --python_out=. scip.proto
from neo4j import GraphDatabase


############################################
# CONFIG
############################################

SCIP_PATH = "index.scip"

NEO4J_URI = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "Hyperswitch@123")


############################################
# SCIP CONSTANTS
############################################

DEFINITION = 1
IMPORT = 2
WRITE_ACCESS = 4
READ_ACCESS = 8


############################################
# LOAD SCIP INDEX
############################################

def load_scip_index(path: str) -> scip_pb2.Index:
    """
    Parse a SCIP index file, handling truncated trailing records gracefully.
    The SCIP wire format is a sequence of partial Index messages (one field per
    record); we merge each complete record and skip any truncated final record.
    """
    from google.protobuf.internal.decoder import _DecodeVarint

    index = scip_pb2.Index()
    with open(path, "rb") as f:
        data = f.read()

    total = len(data)
    pos = 0
    skipped = 0

    while pos < total:
        if pos + 1 > total:
            break
        try:
            tag, after_tag = _DecodeVarint(data, pos)
        except Exception:
            break

        wire_type = tag & 0x7
        if wire_type != 2:
            # Non-length-delimited field — unexpected in SCIP top-level
            break

        try:
            length, after_len = _DecodeVarint(data, after_tag)
        except Exception:
            break

        if after_len + length > total:
            # Truncated record — skip gracefully
            skipped += 1
            break

        chunk = data[pos : after_len + length]
        try:
            index.MergeFromString(chunk)
        except Exception:
            skipped += 1

        pos = after_len + length

    if skipped:
        print(f"  Warning: skipped {skipped} truncated/corrupt record(s) at end of {path}")

    return index


############################################
# SYMBOL KIND MAPPING
############################################

def symbol_kind_name(kind_enum: int) -> str:
    """
    Map SCIP Kind enum integer to a lowercase string name.
    Uses the proto enum directly — version-safe.
    Falls back to 'unknown' for unrecognised values.
    """
    try:
        # .keys() and .values() give name->int, use reverse lookup
        kind_map = {v: k.lower()
                    for k, v in scip_pb2.SymbolInformation.Kind.items()}
        return kind_map.get(kind_enum, "unknown")
    except Exception:
        return "unknown"

def map_label(kind: str) -> str:
    """
    Normalize SCIP symbol kinds into higher-level categories.
    Handles both old proto naming (snake_case) and new proto naming (PascalCase->lowercase).
    """
    k = kind.lower().replace('_', '')  # normalize: abstractmethod = abstract_method

    if k in {"function"}:
        return "Function"

    if k in {"method", "staticmethod", "abstractmethod", "methodreceiver",
             "setter", "getter", "accessor", "subscript"}:
        return "Method"

    if k in {"struct", "class", "message", "datatype"}:
        return "Struct"

    if k in {"trait", "interface", "protocol", "typefamily", "typeclass"}:
        return "Trait"

    if k in {"enum"}:
        return "Enum"

    if k in {"enummember"}:
        return "EnumMember"

    if k in {"field", "staticfield", "staticproperty", "property",
             "instance", "attribute"}:
        return "Field"

    if k in {"parameter", "selfparameter"}:
        return "Parameter"

    if k in {"module", "namespace", "package", "packageobject"}:
        return "Module"

    if k in {"type", "typealias", "typeparameter", "associatedtype",
             "datafamily", "singletonclass"}:
        return "Type"

    if k in {"constant", "value", "boolean", "number", "string"}:
        return "Constant"

    if k in {"macro"}:
        return "Macro"

    if k in {"constructor"}:
        return "Constructor"

    if k in {"impl_block", "implblock"}:
        return "Impl"

    if kind in {"getter", "setter"}:
        return "Accessor"

    if kind in {"class", "singleton_class"}:
        return "Class"

    if kind in {"library"}:
        return "Library"

    if kind in {"file"}:
        return "File"

    return "ExternalSymbol"


############################################
# DISPLAY NAME EXTRACTION
############################################

def extract_display_name(symbol: str) -> str:
    parts = symbol.rstrip(".#()").split("/")
    if parts:
        last = parts[-1]
        if "]" in last:
            last = last.split("]")[-1]
        return last.rstrip("#().")
    return symbol


############################################
# NODE EXTRACTION
############################################

def extract_nodes(index: scip_pb2.Index) -> Dict:
    nodes = {}

    # Pre-compute: for each symbol, which file contains its DEFINITION occurrence.
    # doc.symbols includes imported (cross-crate) symbols, so we can't use
    # doc.relative_path as the authoritative file for every symbol in that list.
    # A DEFINITION occurrence in doc.occurrences is the ground truth for ownership.
    def_file: Dict[str, str] = {}
    for doc in index.documents:
        for occ in doc.occurrences:
            if occ.symbol and not occ.symbol.startswith("local ") and (occ.symbol_roles & DEFINITION):
                def_file[occ.symbol] = doc.relative_path

    for doc in index.documents:

        for sym in doc.symbols:

            symbol = sym.symbol

            if not symbol or symbol.startswith("local "):
                continue

            kind = symbol_kind_name(sym.kind)
            label = map_label(kind)

            # Use the definition file if known; fall back to this document only
            # for symbols that have no definition occurrence anywhere (e.g. stdlib).
            file = def_file.get(symbol, doc.relative_path)

            if symbol not in nodes or symbol in def_file:
                nodes[symbol] = {
                    "symbol": symbol,
                    "label": label,
                    "kind": kind,
                    "name": sym.display_name or extract_display_name(symbol),
                    "file": file,
                    "docs": "\n".join(sym.documentation),
                    "enclosing": sym.enclosing_symbol or None
                }

        for occ in doc.occurrences:

            symbol = occ.symbol

            if not symbol or symbol.startswith("local "):
                continue

            if symbol not in nodes:

                nodes[symbol] = {
                    "symbol": symbol,
                    "label": "ExternalSymbol",
                    "kind": "external",
                    "name": extract_display_name(symbol),
                    "file": def_file.get(symbol),
                    "docs": "",
                    "enclosing": None
                }

    return nodes


############################################
# HIERARCHY EDGES
############################################

def extract_hierarchy_edges(nodes: Dict) -> List:

    edges = []

    for sym, node in nodes.items():

        parent = node["enclosing"]

        if parent:

            edges.append({
                "from": sym,
                "to": parent,
                "type": "BELONGS_TO"
            })

    return edges


############################################
# OCCURRENCE CALL GRAPH
############################################

def extract_occurrence_edges(index) -> List:

    edges = []

    for doc in index.documents:

        definitions = []
        references = []

        for occ in doc.occurrences:

            symbol = occ.symbol
            if not symbol:
                continue

            line = occ.range[0] if occ.range else 0
            is_def = bool(occ.symbol_roles & DEFINITION)

            if is_def and not symbol.startswith("local "):

                definitions.append({
                    "symbol": symbol,
                    "line": line
                })

            elif not symbol.startswith("local "):

                references.append({
                    "symbol": symbol,
                    "line": line,
                    "roles": occ.symbol_roles
                })

        definitions.sort(key=lambda d: d["line"])

        for ref in references:

            parent = find_enclosing_definition(ref["line"], definitions)

            if not parent:
                continue

            edge_type = classify_edge(ref["symbol"], ref["roles"])

            edges.append({
                "from": parent["symbol"],
                "to":   ref["symbol"],
                "type": edge_type,
                "line": ref["line"],          # call/reference site line (1-indexed)
            })

    return edges


############################################
# HELPER FUNCTIONS
############################################

def find_enclosing_definition(line, definitions):
    """Return the innermost function/method definition enclosing `line`.

    Only symbols ending in ``().`` are considered function-level containers;
    parameters (``().(name)``) and other sub-symbols are skipped so that
    a call inside a function body is attributed to the function, not to its
    last declared parameter.
    """
    enclosing = None

    for d in definitions:
        if d["line"] > line:
            break
        # Only treat top-level callable definitions as containers
        sym = d["symbol"]
        if sym.endswith(").") and sym.count("(") >= 1:
            enclosing = d

    return enclosing


def classify_edge(symbol, roles):

    if symbol.endswith("()."):
        return "CALLS"

    if symbol.endswith("#"):
        return "USES_TYPE"

    if roles & WRITE_ACCESS:
        return "WRITES"

    if roles & READ_ACCESS:
        return "READS"

    if roles & IMPORT:
        return "IMPORTS"

    return "REFERENCES"


############################################
# API DETECTION
############################################

ROUTE_RE = re.compile(r'#\[(get|post|put|delete|patch)\("([^"]+)"\)\]')


def detect_api_endpoints(source_code):

    endpoints = []

    for line in source_code.splitlines():

        m = ROUTE_RE.search(line)

        if m:

            endpoints.append({
                "method": m.group(1).upper(),
                "path": m.group(2)
            })

    return endpoints


############################################
# BUILD GRAPH
############################################

def extract_def_lines(index):
    """
    For every definition occurrence, return {symbol: line_0indexed}.
    This is the canonical source-of-truth line for each symbol.
    """
    def_lines = {}
    for doc in index.documents:
        for occ in doc.occurrences:
            if not occ.symbol or occ.symbol.startswith("local "):
                continue
            if bool(occ.symbol_roles & DEFINITION):
                line = occ.range[0] if occ.range else 0
                # Keep the earliest definition (handles multiple cfg variants)
                if occ.symbol not in def_lines or line < def_lines[occ.symbol]:
                    def_lines[occ.symbol] = line
    return def_lines


def extract_trait_dispatch_edges(nodes: Dict, edges_so_far: List) -> List:
    """Precise synthetic CALLS edges for trait method dispatch.

    Problem: SCIP records calls to the abstract trait method (GetTracker#get_trackers)
    when production code uses generic dispatch, so there is no graph edge from an
    endpoint all the way into a concrete impl body.

    Approach (precise, avoids false positives):
    - For each function that has a USES_TYPE edge to an Op struct, and that Op struct
      owns a concrete trait method (struct symbol is a prefix of the method symbol),
      add a direct CALLS edge: caller → concrete_method.
    - This mirrors the compile-time monomorphization: the caller that references
      PaymentConfirm can only dispatch to PaymentConfirm#get_trackers, not to
      PaymentCancel#get_trackers.

    This replaces the over-approximate "abstract → all concretes" approach which
    produced false-positive endpoints (any endpoint reaching the abstract would
    appear to reach every concrete impl).
    """
    # Step 1: find abstract trait methods that are *actually called* in the graph
    # AND are defined within the local project (not stdlib / external crates).
    # Stdlib symbols contain 'https://' in their package descriptor; project symbols
    # use 'cargo <name> <version>' with a plain version string.
    called_syms: set = {e['to'] for e in edges_so_far if e.get('type') == 'CALLS'}

    abstract_tails: set = set()   # e.g. '/GetTracker#get_trackers().'
    for sym in nodes:
        if sym not in called_syms:
            continue
        if 'https://' in sym:          # stdlib / external registry crate — skip
            continue
        parts = sym.split('#')
        # Abstract: exactly 2 '#'-segments, last ends with ').'
        if len(parts) == 2 and parts[-1].endswith(').'):
            tail = sym[sym.rfind('/'):]       # '/TraitName#method().'
            abstract_tails.add(tail)

    if not abstract_tails:
        return []

    # Step 2: find concrete impls corresponding to those abstract methods.
    # Exclude stdlib/external-crate impls — only keep concrete impls whose file
    # belongs to the local project (not rust-lang/rust or other https:// crates).
    # Concrete: 3+ '#'-segments, last ends with ').' and its '/Trait#method().'
    # tail is in abstract_tails.
    concrete_to_struct: Dict[str, str] = {}
    for sym, node in nodes.items():
        if node.get('label') not in ('Function', 'Method'):
            continue
        node_file = node.get('file') or ''
        if not node_file or 'scip_bridge' in node_file:
            continue
        parts = sym.split('#')
        if len(parts) < 3 or not parts[-1].endswith(').'):
            continue
        abstract_tail = '/' + '#'.join(parts[-2:])
        if abstract_tail not in abstract_tails:
            continue
        # Owning struct: prefix up to and including the first '#'
        first_hash = sym.index('#')
        struct_sym = sym[:first_hash + 1]
        if struct_sym in nodes:
            concrete_to_struct[sym] = struct_sym

    if not concrete_to_struct:
        return []

    # Step 3: struct_sym → concrete impl methods
    struct_to_concrete: Dict[str, List[str]] = defaultdict(list)
    for method_sym, st in concrete_to_struct.items():
        struct_to_concrete[st].append(method_sym)

    # Step 4: for each USES_TYPE edge pointing at a relevant struct, add a CALLS
    # edge from the caller to the concrete trait method impl.
    new_edges = []
    for e in edges_so_far:
        if e.get('type') != 'USES_TYPE':
            continue
        to_sym = e['to']
        if to_sym not in struct_to_concrete:
            continue
        caller_sym = e['from']
        for method_sym in struct_to_concrete[to_sym]:
            new_edges.append({
                'from': caller_sym,
                'to':   method_sym,
                'type': 'CALLS',
                'line': None,
            })

    return new_edges


def build_graph(index):

    nodes     = extract_nodes(index)
    def_lines = extract_def_lines(index)

    # Attach def_line (1-indexed) to every node that has a definition
    for symbol, node in nodes.items():
        node["def_line"] = (def_lines[symbol] + 1) if symbol in def_lines else None

    edges = []

    edges += extract_hierarchy_edges(nodes)
    edges += extract_occurrence_edges(index)

    unique = set()
    unique_edges = []

    for e in edges:

        key = (e["from"], e["to"], e["type"])

        if key not in unique:
            unique.add(key)
            unique_edges.append(e)

    return nodes, unique_edges


############################################
# NEO4J LOADER
############################################

def load_into_neo4j(nodes, edges):
    """
    Load nodes and edges into Neo4j.
    - All nodes have label :CodeEntity
    - `label` is stored as a property for querying
    """

    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    node_list = list(nodes.values())

    # Node import query
    node_query = """
    UNWIND $nodes AS node
    MERGE (n:CodeEntity {symbol: node.symbol})
    SET
        n.kind     = node.kind,
        n.label    = node.label,
        n.name     = node.name,
        n.file     = node.file,
        n.docs     = node.docs,
        n.def_line = CASE WHEN node.def_line IS NOT NULL THEN node.def_line ELSE n.def_line END
    """

    # Edge import query — stores call-site line on CALLS edges
    edge_query = """
    UNWIND $edges AS edge
    MATCH (a:CodeEntity {symbol: edge.from})
    MATCH (b:CodeEntity {symbol: edge.to})
    MERGE (a)-[r:RELATION {type: edge.type}]->(b)
    ON CREATE SET r.line = edge.line
    ON MATCH  SET r.line = CASE
        WHEN r.line IS NULL OR edge.line < r.line
        THEN edge.line
        ELSE r.line
    END
    """

    # Helper: batch in chunks
    def chunks(lst, size=10000):
        for i in range(0, len(lst), size):
            yield lst[i:i+size]

    with driver.session() as session:

        print("Clearing graph...")
        session.run("MATCH (n) DETACH DELETE n")

        print("Creating index...")
        session.run("""
        CREATE INDEX code_symbol IF NOT EXISTS
        FOR (n:CodeEntity) ON (n.symbol)
        """)
        session.run("""
        CREATE INDEX code_label IF NOT EXISTS
        FOR (n:CodeEntity) ON (n.label)
        """)
        session.run("""
        CREATE INDEX code_kind IF NOT EXISTS
        FOR (n:CodeEntity) ON (n.kind)
        """)
        session.run("""
        CREATE INDEX code_name IF NOT EXISTS
        FOR (n:CodeEntity) ON (n.name)
        """)

        print("Loading nodes...")
        for batch in chunks(node_list):
            session.run(node_query, nodes=batch)

        print("Loading edges...")
        for batch in chunks(edges):
            session.run(edge_query, edges=batch)

        # Targeted synthetic edges: Op-struct → concrete trait method impls.
        # When a caller USES_TYPE an Op struct (e.g. PaymentConfirm), and that
        # struct owns a concrete trait method impl (its symbol is a prefix of the
        # method symbol), add a CALLS edge caller → concrete_method.
        # This resolves generic/trait dispatch without over-approximating to ALL
        # concrete impls from the abstract trait method.
        print("Adding targeted Op-dispatch edges...")
        # Only add synthetic dispatch edges FROM endpoint handlers.
        # Endpoint handlers are the ones that explicitly pass a concrete Op struct
        # (e.g. payments::PaymentConfirm) to payments_core / authorize_verify_select.
        # Intermediate helpers (call_payment_flow, etc.) may also reference Op structs
        # in type annotations but are NOT the source of the dispatch — restricting to
        # is_endpoint:true avoids false-positive paths through those helpers.
        session.run("""
            MATCH (caller:CodeEntity {is_endpoint:true})-[:RELATION {type:'USES_TYPE'}]->(op:CodeEntity)
            WHERE op.label IN ['Struct','Type']
            MATCH (gt:CodeEntity)
            WHERE gt.label IN ['Function','Method']
              AND gt.symbol STARTS WITH op.symbol
              AND gt.symbol <> op.symbol
              AND gt.file IS NOT NULL
              AND NOT gt.file CONTAINS 'scip_bridge'
            MERGE (caller)-[:RELATION {type:'CALLS', synthetic:true}]->(gt)
        """)

    driver.close()




############################################
# ENDPOINT TAGGING
############################################

def _parse_routes_app(filepath):
    """
    Parse routes/app.rs to extract (method, full_path, handler) triples.

    Structure:
      impl Payments {
        pub fn server(...) -> Scope {
          let mut route = web::scope("/payments").app_data(...);
          route = route
            .service(
              web::resource("/{payment_id}/confirm")
                .route(web::post().to(payments_confirm))
            )
        }
      }

    Strategy: scan for web::scope("PATH"), then collect all
    web::resource("PATH").route(web::METHOD().to(HANDLER)) within that scope block.
    """
    import re
    try:
        with open(filepath, errors='replace') as f:
            content = f.read()
    except OSError:
        return []

    routes = []

    # Find all web::scope("...") occurrences and their enclosing fn block
    scope_re  = re.compile(r'web::scope\(\s*"([^"]*)"\s*\)')
    res_re    = re.compile(r'web::resource\(\s*"([^"]*)"\s*\)')
    route_re  = re.compile(r'web::([a-z]+)\(\)\s*\.to\(([a-zA-Z_][a-zA-Z0-9_:]*)\)')

    lines = content.splitlines()
    n     = len(lines)

    # Build a flat list of (line_idx, type, value) tokens
    tokens = []
    for i, line in enumerate(lines):
        for m in scope_re.finditer(line):
            tokens.append((i, 'scope', m.group(1)))
        for m in res_re.finditer(line):
            tokens.append((i, 'resource', m.group(1)))
        for m in route_re.finditer(line):
            tokens.append((i, 'route', (m.group(1).upper(), m.group(2).split('::')[-1])))

    # Walk tokens: when we see a scope, push it; when we see resource+route pair, emit
    # Use indentation to track scope nesting
    scope_stack = []   # list of (indent, path_prefix)
    pending_resource = None

    for i, (line_idx, ttype, tval) in enumerate(tokens):
        line    = lines[line_idx]
        indent  = len(line) - len(line.lstrip())

        # Pop scopes that are no longer enclosing (higher indent = deeper nesting)
        scope_stack = [(ind, p) for ind, p in scope_stack if ind < indent]

        if ttype == 'scope':
            scope_stack.append((indent, tval))
            pending_resource = None

        elif ttype == 'resource':
            pending_resource = (indent, tval)

        elif ttype == 'route':
            method, handler = tval
            # Build full path from scope stack + resource
            prefix = ''.join(p for _, p in scope_stack)
            resource_path = pending_resource[1] if pending_resource else ''
            full_path = prefix.rstrip('/') + '/' + resource_path.lstrip('/')
            full_path = re.sub(r'/+', '/', full_path)
            if not full_path.startswith('/'):
                full_path = '/' + full_path
            # Normalize {xxx} → {xxx} (already fine), strip trailing slash
            full_path = full_path.rstrip('/') or '/'
            routes.append({'method': method, 'path': full_path, 'handler': handler})

    return routes


def tag_endpoints(driver, src_root):
    """Tag CodeEntity nodes that are HTTP endpoint handlers."""
    if not src_root or not os.path.isdir(src_root):
        print("  Skipping endpoint tagging: SRC_ROOT not set or not found")
        return 0

    routes_app = os.path.join(src_root, 'crates', 'router', 'src', 'routes', 'app.rs')
    if not os.path.exists(routes_app):
        print(f"  routes/app.rs not found at {routes_app}")
        return 0

    routes = _parse_routes_app(routes_app)

    # Deduplicate by handler (keep first registration = v1 variant)
    seen = {}
    for r in routes:
        h = r['handler']
        if h not in seen:
            seen[h] = r

    print(f"  Found {len(routes)} route entries, {len(seen)} unique handlers")

    if not seen:
        return 0

    tagged = 0
    with driver.session() as session:
        for handler, info in seen.items():
            result = session.run("""
                MATCH (n:CodeEntity {name: $name})
                WHERE n.label IN ['Function', 'Method']
                SET n.is_endpoint  = true,
                    n.http_method  = $method,
                    n.http_path    = $path
                RETURN count(n) AS cnt
            """, name=handler, method=info['method'], path=info['path'])
            row = result.single()
            if row and row['cnt'] > 0:
                tagged += row['cnt']

    print(f"  Tagged {tagged} endpoint handler nodes in Neo4j")
    return tagged


############################################
# MAIN
############################################

def main():

    print("Loading SCIP index...")

    index = load_scip_index(SCIP_PATH)

    print("Building semantic graph...")

    nodes, edges = build_graph(index)

    print(f"Nodes: {len(nodes)}")
    print(f"Edges: {len(edges)}")

    print("Loading into Neo4j...")

    load_into_neo4j(nodes, edges)

    print("Done.")

    # Tag endpoint handlers
    src_root = os.environ.get("SRC_ROOT", "")
    if src_root:
        print("Tagging API endpoint handlers...")
        _tag_driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
        tag_endpoints(_tag_driver, src_root)
        _tag_driver.close()
    else:
        print("Set SRC_ROOT env var to tag endpoint handlers.")


if __name__ == "__main__":
    main()