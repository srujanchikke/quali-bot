"""
ts_backprop.py — Tree-sitter based condition extraction and backpropagation.

Replaces the regex/indent heuristics in classify() with a proper CST walk.
The key design principle: backpropagation is DISCRIMINANT-AGNOSTIC.
We trace a variable back to its origin and report what kind of value it is.
The caller decides whether that discriminant enables static Op exclusions.

Discriminant types reported:
  'op_format_dispatch'  — match format!("{op:?}") found; true/false_ops extracted
  'option_gate'         — function returns Option; inner discriminant resolved recursively
  'runtime_data'        — depends on payment_data / config; can't filter statically
  'unknown'             — couldn't trace (depth limit or unrecognised pattern)

Usage:
  from ts_backprop import resolve_call_gates
  gates = resolve_call_gates(lines, call_line, body_start, known_ops, src_root)
  for gate in gates:
      if gate['type'] == 'op_format_dispatch':
          if ep_actual_ops.issubset(gate['false_ops']):
              return True   # rejected
"""

from __future__ import annotations
import os, re
from typing import Optional

from tree_sitter import Language, Parser, Node
import tree_sitter_rust as tsrust

# ── Parser singleton ────────────────────────────────────────────────────────────
RUST_LANG = Language(tsrust.language())
_parser   = Parser(RUST_LANG)

# ── File parse cache ────────────────────────────────────────────────────────────
_tree_cache: dict = {}  # filepath → (mtime, tree, source_bytes)

def _parse_lines(lines: list[str]):
    src = ''.join(lines).encode('utf8', errors='replace')
    return _parser.parse(src), src

def _parse_file(src_root: str, filepath: str):
    full = os.path.join(src_root, filepath.lstrip('/'))
    try:
        mtime = os.path.getmtime(full)
    except OSError:
        return None, None
    if full in _tree_cache and _tree_cache[full][0] == mtime:
        return _tree_cache[full][1], _tree_cache[full][2]
    try:
        src = open(full, errors='replace').read().encode('utf8', errors='replace')
    except OSError:
        return None, None
    tree = _parser.parse(src)
    _tree_cache[full] = (mtime, tree, src)
    return tree, src


# ── Node helpers ────────────────────────────────────────────────────────────────

def _text(node: Node) -> str:
    return node.text.decode('utf8', errors='replace') if node else ''

def _node_at_line(root: Node, line: int) -> Optional[Node]:
    """Return the deepest named node that starts at `line` (0-indexed)."""
    def _walk(node: Node) -> Optional[Node]:
        if node.start_point[0] == line and node.is_named:
            # Descend to find deepest match
            for child in node.named_children:
                if child.start_point[0] == line:
                    deeper = _walk(child)
                    if deeper:
                        return deeper
            return node
        for child in node.named_children:
            if child.start_point[0] <= line <= child.end_point[0]:
                result = _walk(child)
                if result:
                    return result
        return None
    return _walk(root)

def _enclosing_fn_body(node: Node) -> Optional[Node]:
    """Walk up to find the nearest function_item body block."""
    n = node.parent
    while n:
        if n.type == 'function_item':
            return n.child_by_field_name('body')
        n = n.parent
    return None

def _fn_body_for_name(root: Node, fn_name: str) -> Optional[Node]:
    """Find the body block of `fn fn_name` anywhere in `root`."""
    def _walk(node: Node) -> Optional[Node]:
        if node.type == 'function_item':
            name_node = node.child_by_field_name('name')
            if name_node and _text(name_node) == fn_name:
                return node.child_by_field_name('body')
        for child in node.named_children:
            result = _walk(child)
            if result:
                return result
        return None
    return _walk(root)


# ── Enclosing conditions (CST walk up) ─────────────────────────────────────────

def enclosing_conditions(call_node: Node) -> list[dict]:
    """
    Walk up from call_node collecting all enclosing if/match conditions.
    Returns list of condition dicts, outermost first.

    Each dict has:
      type           : 'if' | 'if_let' | 'match_arm'
      condition_node : the condition or pattern Node
      scrutinee_node : for if_let/match_arm, the value being tested (may be None)
      text           : human-readable condition text
    """
    conditions = []
    n = call_node.parent

    while n:
        if n.type == 'if_expression':
            cond = n.child_by_field_name('condition')
            if cond:
                if cond.type == 'let_condition':
                    # if let Pattern = scrutinee
                    scrutinee = cond.child_by_field_name('value')
                    pattern   = cond.child_by_field_name('pattern')
                    conditions.append({
                        'type':           'if_let',
                        'condition_node': cond,
                        'pattern_node':   pattern,
                        'scrutinee_node': scrutinee,
                        'text':           _text(cond),
                    })
                else:
                    conditions.append({
                        'type':           'if',
                        'condition_node': cond,
                        'scrutinee_node': None,
                        'text':           _text(cond),
                    })

        elif n.type == 'match_arm':
            # The match expression is n.parent.parent (match_block → match_expression)
            match_block = n.parent
            match_expr  = match_block.parent if match_block else None
            scrutinee   = None
            if match_expr and match_expr.type == 'match_expression':
                scrutinee = match_expr.child_by_field_name('value')
            pattern = n.child_by_field_name('pattern')
            conditions.append({
                'type':           'match_arm',
                'condition_node': pattern,
                'scrutinee_node': scrutinee,
                'text':           _text(pattern) if pattern else '',
            })

        n = n.parent

    conditions.reverse()   # outermost first
    return conditions


# ── Variable binding resolution ─────────────────────────────────────────────────

def _find_let_binding(fn_body: Node, var_name: str, before_line: int) -> Optional[Node]:
    """
    Find the most recent `let var_name = <value>` in fn_body before `before_line`.
    Returns the value Node (RHS of the let).
    """
    best = None
    best_line = -1

    def _walk(node: Node):
        nonlocal best, best_line
        if node.type == 'let_declaration':
            if node.start_point[0] >= before_line:
                return
            pat = node.child_by_field_name('pattern')
            val = node.child_by_field_name('value')
            if pat and val and _text(pat).strip('&mut ').split('(')[0].strip() == var_name:
                if node.start_point[0] > best_line:
                    best = val
                    best_line = node.start_point[0]
        for child in node.named_children:
            _walk(child)

    _walk(fn_body)
    return best


# ── Op dispatch detection ───────────────────────────────────────────────────────

def _is_format_op_dispatch(scrutinee_node: Node) -> bool:
    """
    Returns True if the scrutinee is `format!("{...}").as_str()` —
    the classic Op-type dispatch pattern.
    """
    if not scrutinee_node:
        return False
    t = scrutinee_node.type
    # match format!("{op:?}").as_str()
    if t == 'call_expression':
        fn_node = scrutinee_node.child_by_field_name('function')
        if fn_node and fn_node.type == 'field_expression':
            field = fn_node.child_by_field_name('field')   # Rust grammar: 'field', not 'name'
            obj   = fn_node.child_by_field_name('value')
            if field and _text(field) == 'as_str' and obj and obj.type == 'macro_invocation':
                macro_name = obj.child_by_field_name('macro')
                if macro_name and _text(macro_name) == 'format':
                    return True
    return False


def _extract_op_dispatch(match_expr_node: Node, known_ops: set[str]) -> dict:
    """
    Parse a `match format!("{op:?}").as_str() { ... }` match expression.
    Returns:
      { true_ops: set, false_ops: set, complex_ops: set, wildcard: 'true'|'false'|'complex'|None }

    - true_ops:    arms with `=> true`  (always included)
    - false_ops:   arms with `=> false` (always excluded)
    - complex_ops: arms with complex body (conservative — keep)
    - wildcard:    what the `_` arm returns
    """
    result = {
        'true_ops':    set(),
        'false_ops':   set(),
        'complex_ops': set(),
        'wildcard':    None,
    }
    body = match_expr_node.child_by_field_name('body')
    if not body:
        return result

    for arm in body.named_children:
        if arm.type != 'match_arm':
            continue
        pattern = arm.child_by_field_name('pattern')
        value   = arm.child_by_field_name('value')
        if not pattern or not value:
            continue

        pat_text = _text(pattern).strip()

        # Wildcard arm
        if pattern.named_child_count == 0 and pat_text == '_':
            if value.type == 'boolean_literal':
                result['wildcard'] = _text(value).strip()  # 'true' or 'false'
            else:
                result['wildcard'] = 'complex'
            continue

        # String literal arm: "PaymentConfirm"
        # pattern → match_pattern → string_literal → string_content
        op_name = None
        def _find_str_content(node):
            if node.type == 'string_content':
                return _text(node)
            for c in node.named_children:
                r = _find_str_content(c)
                if r: return r
            return None

        op_name = _find_str_content(pattern)
        if not op_name or op_name not in known_ops:
            continue

        if value.type == 'boolean_literal':
            lit = _text(value).strip()
            if lit == 'true':
                result['true_ops'].add(op_name)
            elif lit == 'false':
                result['false_ops'].add(op_name)
        else:
            # Complex expression (matches!, block, etc.) — conservative
            result['complex_ops'].add(op_name)

    return result


# ── Backpropagation core ────────────────────────────────────────────────────────

def _backpropagate_node(
    node: Node,
    fn_body: Node,
    root: Node,
    src_root: str,
    known_ops: set[str],
    depth: int = 0,
    max_depth: int = 3,
) -> dict:
    """
    Given a value node (scrutinee, variable RHS, etc.) trace it back to its
    discriminant origin.

    Returns one of:
      {'type': 'op_format_dispatch', 'fn_name': str, 'dispatch': dict}
      {'type': 'runtime_data',       'expr': str}
      {'type': 'unknown'}
    """
    if not node or depth > max_depth:
        return {'type': 'unknown'}

    ntype = node.type
    text  = _text(node)

    # ── Case 1: identifier → resolve via let binding ───────────────────────────
    if ntype == 'identifier':
        var_name   = text.strip()
        before_line = node.start_point[0]
        rhs = _find_let_binding(fn_body, var_name, before_line)
        if rhs:
            return _backpropagate_node(rhs, fn_body, root, src_root, known_ops, depth + 1, max_depth)
        # Not a local variable — likely a function parameter or external
        return {'type': 'runtime_data', 'expr': var_name}

    # ── Case 2: transparent wrappers — unwrap and recurse ─────────────────────
    # try_expression (expr?), await_expression (expr.await), reference_expression (&expr)
    if ntype in ('try_expression', 'await_expression', 'reference_expression'):
        # All three have a single meaningful child (the inner expression)
        inner = None
        if ntype == 'reference_expression':
            inner = node.child_by_field_name('value')
        else:
            # try_expression and await_expression: first named child is the inner expr
            inner = node.named_children[0] if node.named_child_count > 0 else None
        if inner:
            return _backpropagate_node(inner, fn_body, root, src_root, known_ops, depth, max_depth)

    # ── Case 3: call_expression → check the function being called ─────────────
    if ntype == 'call_expression':
        fn_node = node.child_by_field_name('function')
        if not fn_node:
            return {'type': 'unknown'}

        # Sub-case: format!("{op:?}").as_str() — this IS an op dispatch scrutinee
        # (would only reach here if the match scrutinee is a call_expression —
        # handled in the match_arm condition path instead, but handle here too)
        if _is_format_op_dispatch(node):
            return {'type': 'op_format_dispatch_scrutinee', 'node': node}

        # Sub-case: regular function call — look the function up in same file
        fn_name = _extract_simple_fn_name(fn_node)
        if not fn_name:
            return {'type': 'runtime_data', 'expr': text[:60]}

        # Look up the function in the same file's root
        result = _analyze_fn_in_root(fn_name, root, src_root, known_ops, depth + 1, max_depth)
        return result

    # ── Case 4: field_expression (payment_data.status) → runtime data ─────────
    if ntype in ('field_expression', 'scoped_identifier', 'type_cast_expression'):
        return {'type': 'runtime_data', 'expr': text[:60]}

    # ── Case 5: method_call (operation.to_domain(), etc.) ─────────────────────
    if ntype == 'method_call_expression':
        # If receiver is `operation` → could be Op dispatch via method
        receiver = node.child_by_field_name('receiver')
        if receiver and _text(receiver).strip() in ('operation', '&operation'):
            return {'type': 'runtime_data', 'expr': text[:60]}
        return {'type': 'runtime_data', 'expr': text[:60]}

    return {'type': 'unknown'}


def _extract_simple_fn_name(fn_node: Node) -> Optional[str]:
    """Extract a simple function name from a call's function node."""
    if fn_node.type == 'identifier':
        return _text(fn_node)
    # scoped: crate::module::fn_name — take last segment
    if fn_node.type == 'scoped_identifier':
        name_node = fn_node.child_by_field_name('name')
        return _text(name_node) if name_node else None
    # field_expression: obj.method — take field name (method calls handled separately)
    if fn_node.type == 'field_expression':
        field = fn_node.child_by_field_name('name')
        return _text(field) if field else None
    return None


def _analyze_fn_in_root(
    fn_name: str,
    root: Node,
    src_root: str,
    known_ops: set[str],
    depth: int,
    max_depth: int,
) -> dict:
    """
    Find `fn fn_name` in `root` and determine what it returns / dispatches on.

    1. If the function body contains `match format!("{...}").as_str()` →
       return op_format_dispatch with the extracted Op map.
    2. If the function body contains a call to a function that has op dispatch →
       follow transitively (depth-limited).
    3. Otherwise → runtime_data.
    """
    if depth > max_depth:
        return {'type': 'unknown'}

    body = _fn_body_for_name(root, fn_name)
    if not body:
        return {'type': 'unknown'}

    # Walk the body looking for match format!(...).as_str()
    result = _find_op_dispatch_in_body(body, fn_name, known_ops)
    if result:
        return result

    # No direct dispatch — look for calls to other functions that might have it
    # (1-level: only follow functions defined in the same file to avoid ambiguity)
    for call_node in _find_calls_passing_operation(body):
        fn_node = call_node.child_by_field_name('function')
        inner_fn = _extract_simple_fn_name(fn_node) if fn_node else None
        if inner_fn and inner_fn != fn_name:
            inner_body = _fn_body_for_name(root, inner_fn)
            if inner_body:
                r = _find_op_dispatch_in_body(inner_body, inner_fn, known_ops)
                if r:
                    return r

    return {'type': 'runtime_data', 'expr': f'{fn_name}(...)'}


def _find_op_dispatch_in_body(body: Node, fn_name: str, known_ops: set[str]) -> Optional[dict]:
    """
    Search a function body for `match format!("{...}").as_str() { ... }`.
    Returns op_format_dispatch dict or None.
    """
    def _walk(node: Node):
        if node.type == 'match_expression':
            scrutinee = node.child_by_field_name('value')
            if scrutinee and _is_format_op_dispatch(scrutinee):
                dispatch = _extract_op_dispatch(node, known_ops)
                return {'type': 'op_format_dispatch', 'fn_name': fn_name, 'dispatch': dispatch}
        for child in node.named_children:
            r = _walk(child)
            if r: return r
        return None
    return _walk(body)


def _find_calls_passing_operation(body: Node) -> list[Node]:
    """Find all call_expression nodes in body that pass `operation` as an argument."""
    results = []
    _OP_ID = {'operation', '&operation'}

    def _walk(node: Node):
        if node.type == 'call_expression':
            args = node.child_by_field_name('arguments')
            if args:
                for arg in args.named_children:
                    if _text(arg).strip() in _OP_ID or (
                        arg.type == 'reference_expression' and
                        _text(arg.child_by_field_name('value') or arg).strip() == 'operation'
                    ):
                        results.append(node)
                        break
        for child in node.named_children:
            _walk(child)

    _walk(body)
    return results


# ── Main entry point ────────────────────────────────────────────────────────────

def resolve_call_gates(
    lines: list[str],
    call_line: int,
    body_start: int,
    known_ops: set[str],
    src_root: str = '',
) -> list[dict]:
    """
    Given a function's source lines and a call site line (0-indexed), return
    a list of gate dicts describing what conditions gate that call.

    Each gate dict has at minimum a 'type' key.
    The caller checks: if type == 'op_format_dispatch', use gate['dispatch']
    to compute excluded ops.

    Returns [] if no conditions found (call is unconditional).
    """
    tree, src = _parse_lines(lines)
    root = tree.root_node

    # Find the call node at call_line
    call_node = _node_at_line(root, call_line)
    if not call_node:
        return []

    fn_body = _enclosing_fn_body(call_node)
    if not fn_body:
        return []

    conditions = enclosing_conditions(call_node)
    if not conditions:
        return []

    gates = []
    for cond in conditions:
        ctype = cond['type']
        scrutinee = cond.get('scrutinee_node')

        if ctype == 'match_arm':
            # Two sub-cases:
            # A) scrutinee is format!("{op:?}").as_str() → direct Op dispatch
            # B) scrutinee is a variable → backpropagate
            if scrutinee and _is_format_op_dispatch(scrutinee):
                # Direct: the match itself is an Op dispatch
                # Find the match_expression node (parent of match_block, parent of match_arm)
                match_expr = _find_match_expr_for_arm(cond['condition_node'])
                if match_expr:
                    dispatch = _extract_op_dispatch(match_expr, known_ops)
                    # Which arm pattern is this?
                    arm_pat = cond['text']
                    gates.append({
                        'type':         'op_format_dispatch',
                        'fn_name':      '<inline>',
                        'dispatch':     dispatch,
                        'active_arm':   arm_pat,
                        'condition':    cond,
                    })
            elif scrutinee:
                result = _backpropagate_node(scrutinee, fn_body, root, src_root, known_ops)
                if result['type'] == 'op_format_dispatch':
                    result['active_arm'] = cond['text']
                    result['condition']  = cond
                gates.append(result)

        elif ctype == 'if_let':
            # if let Some(x) = scrutinee → trace scrutinee
            if scrutinee:
                result = _backpropagate_node(scrutinee, fn_body, root, src_root, known_ops)
                if result['type'] == 'op_format_dispatch':
                    result['condition'] = cond
                    result['if_let_pattern'] = cond.get('text', '')
                gates.append(result)

        elif ctype == 'if':
            # if condition_fn(operation, ...) or if boolean_condition
            # Check if the condition itself contains a function call passing operation
            cond_node = cond.get('condition_node')
            if cond_node:
                calls = _find_calls_passing_operation(cond_node)
                for call in calls:
                    fn_node = call.child_by_field_name('function')
                    fn_name = _extract_simple_fn_name(fn_node) if fn_node else None
                    if fn_name:
                        result = _analyze_fn_in_root(fn_name, root, src_root, known_ops, 0, 3)
                        if result['type'] == 'op_format_dispatch':
                            result['condition'] = cond
                            gates.append(result)

    return [g for g in gates if g.get('type') != 'unknown']


def _find_match_expr_for_arm(pattern_node: Node) -> Optional[Node]:
    """Walk up from a match arm pattern to find the parent match_expression."""
    n = pattern_node
    while n:
        if n.type == 'match_expression':
            return n
        n = n.parent
    return None


# ── Op exclusion computation ────────────────────────────────────────────────────

def compute_op_exclusions(gate: dict, known_ops: set[str]) -> frozenset[str]:
    """
    Given a gate dict of type 'op_format_dispatch', compute the set of Ops
    that are DEFINITELY excluded (always-false path).

    For match_arm gates: only Ops that produce the ACTIVE arm are included.
    For if/if_let gates with `_ => false`: all unlisted Ops are excluded.
    """
    if gate.get('type') != 'op_format_dispatch':
        return frozenset()

    dispatch = gate.get('dispatch', {})
    true_ops    = dispatch.get('true_ops',    set())
    false_ops   = dispatch.get('false_ops',   set())
    complex_ops = dispatch.get('complex_ops', set())
    wildcard    = dispatch.get('wildcard')    # 'true', 'false', 'complex', or None

    listed_ops = true_ops | false_ops | complex_ops

    # For match_arm conditions (e.g. ConnectorCallType::Retryable arm):
    # the active_arm tells us WHICH variant the path requires.
    # But the op_format_dispatch is about what Op is used — if the Op is always
    # excluded by the gate function, the path is unreachable.
    # For a boolean gate (should_call_connector → true/false):
    exclusions = set(false_ops)
    if wildcard == 'false':
        exclusions |= (known_ops - listed_ops)

    return frozenset(exclusions)


# ── Standalone test ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys

    src_root = os.environ.get('SRC_ROOT', '')
    fn_name  = sys.argv[1] if len(sys.argv) > 1 else 'get_connector_with_networks'

    # Load the payments.rs file and test on a known call
    payments_file = 'crates/router/src/core/payments.rs'
    full_path     = os.path.join(src_root, payments_file)
    try:
        lines = open(full_path, errors='replace').readlines()
    except OSError:
        print(f"Could not read {full_path}")
        sys.exit(1)

    # Find call to get_connector_data_with_routing_decision inside payments_operation_core
    # (this is the hop that gates get_connector_with_networks)
    target_call = 'get_connector_data_with_routing_decision'
    call_line = None
    for i, line in enumerate(lines):
        if target_call in line and 'fn ' not in line:
            call_line = i
            break

    if call_line is None:
        print(f"Could not find call to {target_call}")
        sys.exit(1)

    print(f"Testing backpropagation at line {call_line + 1} ({target_call})")
    print()

    # Fetch known ops (hardcode for standalone test)
    known_ops = {
        'PaymentConfirm', 'PaymentCreate', 'PaymentUpdate', 'PaymentSession',
        'PaymentCancel', 'PaymentCapture', 'PaymentStatus', 'PaymentStart',
        'CompleteAuthorize', 'PaymentApprove', 'PaymentReject',
        'PaymentSessionUpdate', 'PaymentPostSessionTokens', 'PaymentUpdateMetadata',
        'PaymentExtendAuthorization', 'PaymentIncrementalAuthorization',
        'PaymentSessionIntent', 'PaymentCancelPostCapture',
    }

    gates = resolve_call_gates(lines, call_line, 0, known_ops, src_root)

    if not gates:
        print("No gates detected (call appears unconditional)")
    else:
        print(f"{len(gates)} gate(s) detected:\n")
        for i, g in enumerate(gates):
            print(f"  Gate {i+1}: type={g['type']}")
            if 'condition' in g:
                print(f"    condition: {g['condition'].get('text', '')[:80]}")
            if 'fn_name' in g:
                print(f"    gate_fn:   {g['fn_name']}")
            if 'active_arm' in g:
                print(f"    arm:       {g['active_arm']}")
            if g['type'] == 'op_format_dispatch':
                d = g.get('dispatch', {})
                excl = compute_op_exclusions(g, known_ops)
                print(f"    true_ops:    {sorted(d.get('true_ops', set()))}")
                print(f"    false_ops:   {sorted(d.get('false_ops', set()))}")
                print(f"    complex_ops: {sorted(d.get('complex_ops', set()))}")
                print(f"    wildcard:    {d.get('wildcard')}")
                print(f"    → EXCLUDED:  {sorted(excl)}")
            elif g['type'] == 'runtime_data':
                print(f"    expr:      {g.get('expr', '?')} (runtime-dependent, kept)")
            print()
