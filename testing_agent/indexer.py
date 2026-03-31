"""
Hyperswitch Cypress Custom Indexer
====================================
Reads the cypress-tests repo directly from disk. No SCIP, no external toolchain.

Re-index strategy
-----------------
  Full rebuild    : Indexer.run(incremental=False)
  Incremental     : Indexer.run()                     — skips unchanged files
  After a patch   : Indexer.reindex_files([path, ...]) — only the touched files

  The agent calls reindex_files() immediately after writing a code change.
  Single-file re-index runs in < 100ms.

CLI
---
  # first time / clean rebuild
  python -m hyperswitch_indexer.indexer --repo /path/to/cypress-tests --full

  # normal startup (skips unchanged files)
  python -m hyperswitch_indexer.indexer --repo /path/to/cypress-tests

  # after patching Stripe.js and Utils.js
  python -m hyperswitch_indexer.indexer --repo /path/to/cypress-tests \\
      --reindex cypress/e2e/configs/Payment/Stripe.js \\
                cypress/e2e/configs/Payment/Utils.js
"""

import re, os, json, hashlib, time, argparse
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
from neo4j import GraphDatabase

# ── domain knowledge ──────────────────────────────────────────────────────────

FLOW_ENDPOINTS: dict[str, list[dict]] = {
    "PaymentIntent":                   [{"method": "POST", "path": "/payments"}],
    "PaymentIntentOffSession":         [{"method": "POST", "path": "/payments"}],
    "PaymentIntentWithShippingCost":   [{"method": "POST", "path": "/payments"}],
    "Capture":                         [{"method": "POST", "path": "/payments/:id/capture"}],
    "PartialCapture":                  [{"method": "POST", "path": "/payments/:id/capture"}],
    "Overcapture":                     [{"method": "POST", "path": "/payments/:id/capture"}],
    "IncrementalAuth":                 [{"method": "POST", "path": "/payments/:id/incremental_authorization"}],
    "Refund":                          [{"method": "POST", "path": "/refunds"}],
    "PartialRefund":                   [{"method": "POST", "path": "/refunds"}],
    "SyncRefund":                      [{"method": "GET",  "path": "/refunds/:id"}],
    "VoidAfterConfirm":                [{"method": "POST", "path": "/payments/:id/cancel"}],
    "ZeroAuthPaymentIntent":           [{"method": "POST", "path": "/payments"}],
    "ZeroAuthConfirmPayment":          [{"method": "POST", "path": "/payments/:id/confirm"}],
    "ZeroAuthMandate":                 [{"method": "POST", "path": "/payments"}],
    "MITAutoCapture":                  [{"method": "POST", "path": "/payments"}],
    "MITManualCapture":                [{"method": "POST", "path": "/payments"}],
    "SaveCardConfirmAutoCaptureOffSession":               [{"method": "POST", "path": "/payments"}],
    "SaveCardConfirmManualCaptureOffSession":             [{"method": "POST", "path": "/payments"}],
    "SaveCardConfirmAutoCaptureOffSessionWithoutBilling": [{"method": "POST", "path": "/payments"}],
    "ManualRetryPaymentEnabled":       [{"method": "POST", "path": "/payments"}],
    "ManualRetryPaymentDisabled":      [{"method": "POST", "path": "/payments"}],
    "ManualRetryPaymentCutoffExpired": [{"method": "POST", "path": "/payments"}],
    "SessionToken":                    [{"method": "POST", "path": "/payments/session_tokens"}],
    "Ideal":      [{"method": "POST", "path": "/payments"}],
    "Eps":        [{"method": "POST", "path": "/payments"}],
    "Giropay":    [{"method": "POST", "path": "/payments"}],
    "Sofort":     [{"method": "POST", "path": "/payments"}],
    "Blik":       [{"method": "POST", "path": "/payments"}],
    "Przelewy":   [{"method": "POST", "path": "/payments"}],
    "Ach":        [{"method": "POST", "path": "/payments"}],
    "Pix":        [{"method": "POST", "path": "/payments"}],
    "DuitNow":    [{"method": "POST", "path": "/payments"}],
    "UpiCollect": [{"method": "POST", "path": "/payments"}],
    "UpiIntent":  [{"method": "POST", "path": "/payments"}],
    "Bluecode":   [{"method": "POST", "path": "/payments"}],
    "CryptoCurrency":              [{"method": "POST", "path": "/payments"}],
    "CryptoCurrencyManualCapture": [{"method": "POST", "path": "/payments"}],
    "PaymentWithBilling":          [{"method": "POST", "path": "/payments"}],
    "PaymentWithBillingEmail":     [{"method": "POST", "path": "/payments"}],
    "PaymentWithFullName":         [{"method": "POST", "path": "/payments"}],
    "PaymentWithoutBilling":       [{"method": "POST", "path": "/payments"}],
    "PaymentConfirmWithShippingCost": [{"method": "POST", "path": "/payments/:id/confirm"}],
}

# All known flow names — used to detect which flows a spec exercises
# by scanning for  getConnectorDetails(...)["card_pm"]["FlowName"]  patterns.
# Add new flow names here when new connector config keys are introduced.
KNOWN_FLOW_NAMES: frozenset[str] = frozenset({
    "PaymentIntent", "PaymentIntentOffSession", "PaymentIntentWithShippingCost",
    "Capture", "PartialCapture", "Overcapture", "IncrementalAuth",
    "Refund", "PartialRefund", "SyncRefund",
    "VoidAfterConfirm",
    "ZeroAuthPaymentIntent", "ZeroAuthConfirmPayment", "ZeroAuthMandate",
    "MITAutoCapture", "MITManualCapture",
    "SaveCardConfirmAutoCaptureOffSession",
    "SaveCardConfirmManualCaptureOffSession",
    "SaveCardConfirmAutoCaptureOffSessionWithoutBilling",
    "ManualRetryPaymentEnabled", "ManualRetryPaymentDisabled",
    "ManualRetryPaymentCutoffExpired",
    "SessionToken",
    "Ideal", "Eps", "Giropay", "Sofort", "Blik", "Przelewy",
    "Ach", "Pix", "DuitNow", "UpiCollect", "UpiIntent",
    "Bluecode", "CryptoCurrency", "CryptoCurrencyManualCapture",
    "PaymentConfirmWithShippingCost",
    "No3DSManualCapture", "No3DSAutoCapture",
})

_FLOW_KEY_RE  = re.compile(r'\["([A-Z][A-Za-z0-9]+)"\]')
_COMMAND_RE   = re.compile(r'cy\.([a-z][A-Za-z]+)(?:CallTest|Test)\s*\(')

# Maps lowercase cy.command substrings → flow names
# e.g. cy.incrementalAuthorizationCallTest → "incrementalauthorization" → IncrementalAuth
_COMMAND_TO_FLOW: dict[str, str] = {
    "incrementalauthorization": "IncrementalAuth",
    "overcapture":              "Overcapture",
    "partialcapture":           "PartialCapture",
    "capture":                  "Capture",
    "partialrefund":            "PartialRefund",
    "syncrefund":               "SyncRefund",
    "refund":                   "Refund",
    "void":                     "VoidAfterConfirm",
    "zeroauthconfirm":          "ZeroAuthConfirmPayment",
    "zeroauthpaymentintent":    "ZeroAuthPaymentIntent",
    "mandate":                  "ZeroAuthMandate",
    "mit":                      "MITAutoCapture",
    "createpaymentintent":      "PaymentIntent",
    "confirm":                  "PaymentIntent",
}


def detect_flows_from_spec(spec_text: str) -> list[str]:
    """
    Detect which flows a spec exercises via two patterns:

    1. Config key:  getConnectorDetails(...)["card_pm"]["IncrementalAuth"]
    2. Command:     cy.incrementalAuthorizationCallTest(...)
                    cy.captureCallTest(...)

    Pattern 2 is needed because some specs call cypress commands directly
    rather than reading from connectorDetails config.
    """
    found = set()

    # Pattern 1: connectorDetails config key
    for m in _FLOW_KEY_RE.finditer(spec_text):
        if m.group(1) in KNOWN_FLOW_NAMES:
            found.add(m.group(1))

    # Pattern 2: cy.someFlowCallTest() or cy.someFlowTest()
    for m in _COMMAND_RE.finditer(spec_text):
        cmd_lower = m.group(1).lower()
        for key, flow in _COMMAND_TO_FLOW.items():
            if key in cmd_lower:
                found.add(flow)
                break

    return sorted(found)


# SPEC_FLOWS is built at parse time from the actual file content.
# Kept as a module-level cache so other modules can import it after indexing.
# Populated by CypressParser.parse_spec() — do not edit manually.
SPEC_FLOWS: dict[str, list[str]] = {}

# ── JS parsing helpers ────────────────────────────────────────────────────────

def _extract_braced(js: str, start: int) -> Optional[str]:
    """From `start` (must be a `{`), return the complete balanced `{...}` block."""
    depth = 0
    for i, ch in enumerate(js[start:]):
        if ch == '{':   depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return js[start : start + i + 1]
    return None


def extract_export(js: str, name: str) -> Optional[str]:
    """
    Extract the value block for:
      export const <name> = { ... }
      const <name> = { ... }
      module.exports.<name> = { ... }
    """
    patterns = [
        rf'(?:export\s+)?(?:const|let|var)\s+{re.escape(name)}\s*=\s*(\{{)',
        rf'module\.exports\.{re.escape(name)}\s*=\s*(\{{)',
    ]
    for pat in patterns:
        m = re.search(pat, js)
        if m:
            return _extract_braced(js, m.start(1))
    return None


def extract_property(js: str, key: str) -> Optional[str]:
    """Extract `key: { ... }` property block."""
    m = re.search(rf'\b{re.escape(key)}\s*:\s*(\{{)', js)
    if not m:
        return None
    return _extract_braced(js, m.start(1))


def extract_all_flows(connector_js: str) -> list[tuple[str, str]]:
    """
    Return [(flow_name, raw_block), ...] for every flow found across all
    payment method groups (card_pm, bank_transfer_pm, bank_redirect_pm, …).
    A flow must have both a Request and a Response block.
    """
    cd = extract_export(connector_js, "connectorDetails")
    if not cd:
        return []

    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    # Scan all PascalCase keys anywhere in connectorDetails
    for m in re.finditer(r'\b([A-Z][A-Za-z0-9]+)\s*:\s*(\{)', cd):
        name = m.group(1)
        if name in ('Request', 'Response', 'Configs', 'ResponseCustom') or name in seen:
            continue
        block = _extract_braced(cd, m.start(2))
        if block and 'Request' in block and 'Response' in block:
            results.append((name, block))
            seen.add(name)

    return results


def extract_response_status(block: str) -> Optional[int]:
    resp = extract_property(block, "Response")
    if not resp:
        return None
    m = re.search(r'\bstatus\s*:\s*(\d{3})\b', resp)
    return int(m.group(1)) if m else None


def extract_request_fields(block: str) -> dict:
    req = extract_property(block, "Request")
    if not req:
        return {}
    inner = req[1:-1].strip()
    fields: dict = {}
    for m in re.finditer(r'\b(\w+)\s*:\s*([^,{}\n]+)', inner):
        k, v = m.group(1), m.group(2).strip().rstrip(',')
        fields[k] = v
    return fields


def parse_connector_lists(utils_js: str) -> dict[str, list[str]]:
    """Return {'INCLUDE.OVERCAPTURE': ['adyen', 'stripe'], ...}"""
    cl = extract_export(utils_js, "CONNECTOR_LISTS")
    if not cl:
        return {}

    result: dict[str, list[str]] = {}
    for group in ("INCLUDE", "EXCLUDE"):
        block = extract_property(cl, group)
        if not block:
            continue
        for m in re.finditer(r'\b([A-Z_]+)\s*:\s*\[([^\]]*)\]', block, re.DOTALL):
            key = m.group(1)
            raw = m.group(2)
            # Strip JS single-line comments (// ...) before splitting
            raw = re.sub(r'//[^\n]*', '', raw)
            vals = [v.strip().strip("\"'") for v in raw.split(",")
                    if v.strip().strip("\"'")]
            result[f"{group}.{key}"] = vals

    return result


def parse_it_blocks(spec_js: str) -> list[dict]:
    tests = []
    for m in re.finditer(r'\b(x?it)\s*\(\s*["`]([^"`]+)["`]', spec_js):
        tests.append({
            "name":    m.group(2),
            "skipped": m.group(1) == "xit",
            "line":    spec_js[: m.start()].count("\n") + 1,
        })
    return tests


def parse_describe_blocks(spec_js: str) -> list[str]:
    return re.findall(r'\bdescribe\s*\(\s*["`]([^"`]+)["`]', spec_js)


def parse_imports(spec_js: str) -> list[str]:
    return re.findall(r'(?:require|import)\s*\(?\s*["\']([^"\']+)["\']', spec_js)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# ── data classes ──────────────────────────────────────────────────────────────

@dataclass
class FlowConfig:
    connector:             str
    flow:                  str
    raw_block:             str
    response_status:       Optional[int]
    request_fields:        dict
    has_amount_to_capture: bool
    has_enable_overcapture: bool
    endpoints:             list[dict] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.connector}::{self.flow}"


@dataclass
class ConnectorFile:
    name:         str
    path:         str
    file_hash:    str
    flows:        list[FlowConfig]
    card_details: dict   # raw JS blocks for successful/failed card details


@dataclass
class TestCase:
    id:          str   # "{spec_name}::{test_name}"
    spec_name:   str
    spec_file:   str
    suite:       str
    test_name:   str
    line:        int
    skipped:     bool
    flows:       list[str]


@dataclass
class SpecFile:
    name:         str
    path:         str
    suite:        str
    file_hash:    str
    test_cases:   list[TestCase]
    imports:      list[str]
    describes:    list[str]


# ── parser ────────────────────────────────────────────────────────────────────

class CypressParser:
    def __init__(self, repo_root: str):
        self.root = Path(repo_root)

    def parse_connector(self, path: Path) -> ConnectorFile:
        js    = path.read_text(encoding="utf-8", errors="replace")
        name  = path.stem

        flows: list[FlowConfig] = []
        for flow_name, block in extract_all_flows(js):
            req_fields = extract_request_fields(block)
            flows.append(FlowConfig(
                connector             = name,
                flow                  = flow_name,
                raw_block             = block,
                response_status       = extract_response_status(block),
                request_fields        = req_fields,
                has_amount_to_capture = "amount_to_capture"  in block,
                has_enable_overcapture= "enable_overcapture" in block,
                endpoints             = FLOW_ENDPOINTS.get(flow_name, []),
            ))

        card_details: dict = {}
        for key in ("successfulNo3DSCardDetails", "successfulThreeDSTestCardDetails",
                    "failedNo3DSCardDetails"):
            obj = extract_export(js, key)
            if obj:
                card_details[key] = obj

        return ConnectorFile(
            name=name, path=str(path.relative_to(self.root)),
            file_hash=file_hash(path), flows=flows, card_details=card_details,
        )

    def parse_utils(self, path: Path) -> dict[str, list[str]]:
        return parse_connector_lists(path.read_text(encoding="utf-8", errors="replace"))

    def parse_spec(self, path: Path) -> SpecFile:
        js    = path.read_text(encoding="utf-8", errors="replace")
        name  = re.sub(r"\.cy$", "", path.stem)
        suite = ("Payout" if "/Payout/" in str(path)
                 else "Routing" if "/Routing/" in str(path)
                 else "Payment")

        # Detect flows dynamically from file content, not a hardcoded map
        flows = detect_flows_from_spec(js)
        # Update the module-level cache so other modules can query it
        SPEC_FLOWS[name] = flows
        raw_tests  = parse_it_blocks(js)
        test_cases = [
            TestCase(
                id        = f"{name}::{t['name']}",
                spec_name = name,
                spec_file = str(path.relative_to(self.root)),
                suite     = suite,
                test_name = t["name"],
                line      = t["line"],
                skipped   = t["skipped"],
                flows     = flows,
            )
            for t in raw_tests
        ]

        return SpecFile(
            name=name, path=str(path.relative_to(self.root)),
            suite=suite, file_hash=file_hash(path),
            test_cases=test_cases, imports=parse_imports(js),
            describes=parse_describe_blocks(js),
        )

    def find_connector_files(self) -> list[Path]:
        base = self.root / "cypress" / "e2e" / "configs" / "Payment"
        return sorted([
            p for p in base.glob("*.js")
            if p.exists() and p.name not in ("Commons.js", "Utils.js", "Modifiers.js")
        ]) if base.exists() else []

    def find_spec_files(self) -> list[Path]:
        specs: list[Path] = []
        for suite in ("Payment", "Payout", "Routing"):
            d = self.root / "cypress" / "e2e" / "spec" / suite
            if d.exists():
                specs.extend(sorted(d.glob("*.cy.js")))
        return specs

    def find_utils(self) -> Optional[Path]:
        p = self.root / "cypress" / "e2e" / "configs" / "Payment" / "Utils.js"
        return p if p.exists() else None


# ── Neo4j writer ──────────────────────────────────────────────────────────────

# ── connector name normalisation map ─────────────────────────────────────────
# Utils.js stores connector names lowercase ("adyen", "bankofamerica").
# Config filenames are PascalCase ("Adyen.js", "BankOfAmerica.js").
# All Neo4j Connector nodes use PascalCase. This map normalises any input.
CONNECTOR_NAME_MAP: dict[str, str] = {
    "aci": "Aci", "adyen": "Adyen", "airwallex": "Airwallex",
    "archipel": "Archipel", "authipay": "Authipay",
    "authorizedotnet": "Authorizedotnet", "bambora": "Bambora",
    "bamboraapac": "Bamboraapac", "bankofamerica": "BankOfAmerica",
    "barclaycard": "Barclaycard", "billwerk": "Billwerk",
    "bluesnap": "Bluesnap", "braintree": "Braintree", "calida": "Calida",
    "cashtocode": "Cashtocode", "celero": "Celero", "checkbook": "Checkbook",
    "checkout": "Checkout", "cryptopay": "Cryptopay",
    "cybersource": "Cybersource", "datatrans": "Datatrans",
    "deutschebank": "Deutschebank", "dlocal": "Dlocal", "elavon": "Elavon",
    "facilitapay": "Facilitapay", "finix": "Finix", "fiserv": "Fiserv",
    "fiservemea": "Fiservemea", "fiuu": "Fiuu", "forte": "Forte",
    "getnet": "Getnet", "gigadat": "Gigadat", "globalpay": "Globalpay",
    "hipay": "Hipay", "iatapay": "Iatapay", "itaubank": "ItauBank",
    "jpmorgan": "Jpmorgan", "loonio": "Loonio", "mollie": "Mollie",
    "moneris": "Moneris", "multisafepay": "Multisafepay",
    "nexinets": "Nexinets", "nexixpay": "Nexixpay", "nmi": "Nmi",
    "noon": "Noon", "novalnet": "Novalnet", "nuvei": "Nuvei",
    "paybox": "Paybox", "payload": "Payload", "paypal": "Paypal",
    "paysafe": "Paysafe", "payu": "Payu", "peachpayments": "Peachpayments",
    "powertranz": "PowerTranz", "redsys": "Redsys", "shift4": "Shift4",
    "silverflow": "Silverflow", "square": "Square", "stax": "Stax",
    "stripe": "Stripe", "tesouro": "Tesouro", "trustpay": "Trustpay",
    "trustpayments": "TrustPayments", "tsys": "Tsys", "volt": "Volt",
    "wellsfargo": "WellsFargo", "worldpay": "WorldPay",
    "worldpayvantiv": "Worldpayvantiv", "worldpayxml": "Worldpayxml",
    "xendit": "Xendit", "zift": "Zift",
}

CONSTRAINTS = [
    "CREATE CONSTRAINT IF NOT EXISTS FOR (c:Connector)    REQUIRE c.name  IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (f:Flow)          REQUIRE f.name  IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (fc:FlowConfig)  REQUIRE fc.id   IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (e:Endpoint)      REQUIRE e.id    IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (s:SpecFile)      REQUIRE s.name  IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (t:TestCase)      REQUIRE t.id    IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (l:FeatureList)  REQUIRE l.key   IS UNIQUE",
]


class Neo4jWriter:
    def __init__(self, uri: str, auth: tuple):
        self.driver = GraphDatabase.driver(uri, auth=auth)

    def close(self):
        self.driver.close()

    def setup(self):
        with self.driver.session() as s:
            for q in CONSTRAINTS:
                s.run(q)

    def clear_all(self):
        with self.driver.session() as s:
            # Delete all nodes and relationships
            s.run("MATCH (n) DETACH DELETE n")
            # Drop all constraints so stale unique indexes don't block reindex
            for label in ("Connector", "Flow", "FlowConfig", "Endpoint",
                          "SpecFile", "TestCase", "FeatureList"):
                try:
                    s.run(f"DROP CONSTRAINT ON (n:{label}) IF EXISTS")
                except Exception:
                    pass  # older Neo4j syntax
            # Re-setup constraints fresh
        self.setup()

    def clear_connector(self, name: str):
        with self.driver.session() as s:
            s.run("""
                MATCH (c:Connector {name:$n})
                OPTIONAL MATCH (c)-[:HAS_CONFIG]->(fc:FlowConfig)
                DETACH DELETE fc, c
            """, n=name)

    def clear_spec(self, name: str):
        with self.driver.session() as s:
            s.run("""
                MATCH (s:SpecFile {name:$n})
                OPTIONAL MATCH (s)-[:HAS_TEST]->(t:TestCase)
                DETACH DELETE t, s
            """, n=name)

    def write_connector(self, cf: ConnectorFile):
        with self.driver.session() as s:
            s.run("""
                MERGE (c:Connector {name:$name})
                SET c.path=$path, c.file_hash=$fh
            """, name=cf.name, path=cf.path, fh=cf.file_hash)

            for fc in cf.flows:
                s.run("MERGE (f:Flow {name:$n})", n=fc.flow)

                for ep in fc.endpoints:
                    ep_id = f"{ep['method']}:{ep['path']}"
                    s.run("""
                        MERGE (e:Endpoint {id:$id})
                        SET e.method=$m, e.path=$p
                        WITH e
                        MATCH (f:Flow {name:$flow})
                        MERGE (f)-[:MAPS_TO]->(e)
                    """, id=ep_id, m=ep["method"], p=ep["path"], flow=fc.flow)

                s.run("""
                    MERGE (fc:FlowConfig {id:$id})
                    SET fc.connector=$conn,
                        fc.flow=$flow,
                        fc.raw_block=$rb,
                        fc.response_status=$rs,
                        fc.request_fields=$rq,
                        fc.has_amount_to_capture=$atc,
                        fc.has_enable_overcapture=$eoc
                    WITH fc
                    MATCH (c:Connector {name:$conn})
                    MERGE (c)-[:HAS_CONFIG]->(fc)
                    WITH fc
                    MATCH (f:Flow {name:$flow})
                    MERGE (fc)-[:FOR_FLOW]->(f)
                    WITH fc
                    MATCH (c:Connector {name:$conn})
                    MATCH (fl:Flow {name:$flow})
                    MERGE (c)-[:SUPPORTS_FLOW]->(fl)
                """,
                    id=fc.id, conn=fc.connector, flow=fc.flow,
                    rb=fc.raw_block[:2000],
                    rs=fc.response_status,
                    rq=json.dumps(fc.request_fields),
                    atc=fc.has_amount_to_capture,
                    eoc=fc.has_enable_overcapture,
                )

    def write_spec(self, sf: SpecFile):
        with self.driver.session() as s:
            s.run("""
                MERGE (sp:SpecFile {name:$name})
                SET sp.path=$path, sp.suite=$suite, sp.file_hash=$fh
            """, name=sf.name, path=sf.path, suite=sf.suite, fh=sf.file_hash)

            for tc in sf.test_cases:
                s.run("""
                    MERGE (t:TestCase {id:$id})
                    SET t.spec_name=$sn, t.spec_file=$sf,
                        t.suite=$suite, t.test_name=$tn,
                        t.line=$line, t.skipped=$skip
                    WITH t
                    MATCH (sp:SpecFile {name:$sn})
                    MERGE (sp)-[:HAS_TEST]->(t)
                """,
                    id=tc.id, sn=tc.spec_name, sf=tc.spec_file,
                    suite=tc.suite, tn=tc.test_name,
                    line=tc.line, skip=tc.skipped,
                )
                for flow in tc.flows:
                    s.run("""
                        MATCH (t:TestCase {id:$tid})
                        MERGE (f:Flow {name:$flow})
                        MERGE (t)-[:TESTS_FLOW]->(f)
                    """, tid=tc.id, flow=flow)
                    for ep in FLOW_ENDPOINTS.get(flow, []):
                        ep_id = f"{ep['method']}:{ep['path']}"
                        s.run("""
                            MATCH (t:TestCase {id:$tid})
                            MERGE (e:Endpoint {id:$eid})
                            SET e.method=$m, e.path=$p
                            MERGE (t)-[:CALLS_ENDPOINT]->(e)
                        """, tid=tc.id, eid=ep_id, m=ep["method"], p=ep["path"])

    def write_connector_lists(self, lists: dict[str, list[str]]):
        with self.driver.session() as s:
            for key, connectors in lists.items():
                # Normalise to PascalCase so MERGE hits the same Connector node
                # that write_connector() created from the filename.
                # Utils.js stores lowercase ("adyen") but nodes are "Adyen".
                pascal = [CONNECTOR_NAME_MAP.get(c.lower(), c) for c in connectors]
                s.run("""
                    MERGE (l:FeatureList {key:$key})
                    SET l.connectors=$conn
                """, key=key, conn=pascal)
                for cname in pascal:
                    s.run("""
                        MATCH (l:FeatureList {key:$key})
                        MERGE (c:Connector {name:$cname})
                        MERGE (c)-[:IN_LIST]->(l)
                    """, key=key, cname=cname)


# ── hash cache ────────────────────────────────────────────────────────────────

class HashCache:
    """
    Persists SHA256 hashes of indexed files so incremental runs skip
    files that haven't changed.
    """
    def __init__(self, path: str = ".indexer_cache.json"):
        self.path  = Path(path)
        self._data: dict[str, str] = json.loads(self.path.read_text()) if self.path.exists() else {}

    def changed(self, path: Path) -> bool:
        return self._data.get(str(path)) != file_hash(path)

    def mark(self, path: Path):
        self._data[str(path)] = file_hash(path)
        
    def unmark(self, path: Path):
        self._data.pop(str(path), None)

    def save(self):
        self.path.write_text(json.dumps(self._data, indent=2))


# ── indexer ───────────────────────────────────────────────────────────────────

class Indexer:
    def __init__(self, repo_root: str, neo4j_uri: str, neo4j_auth: tuple,
                 cache_path: str = None):
        self.parser    = CypressParser(repo_root)
        self.writer    = Neo4jWriter(neo4j_uri, neo4j_auth)
        self.root      = Path(repo_root)
        # Default cache lives inside the repo root so it's always found
        # regardless of which directory you run the script from
        _cache = cache_path or str(self.root / ".indexer_cache.json")
        self.cache     = HashCache(_cache)

    def run(self, incremental: bool = True):
        """
        Index the whole repo.
        incremental=True  → skip files whose hash hasn't changed (default)
        incremental=False → clear graph first, re-index everything
        """
        t0 = time.time()
        self.writer.setup()
        if not incremental:
            self.writer.clear_all()

        ic = sc = is_ = ss = 0   # indexed/skipped counts

        # connector configs
        for path in self.parser.find_connector_files():
            if incremental and not self.cache.changed(path):
                sc += 1; continue
            cf = self.parser.parse_connector(path)
            if incremental:
                self.writer.clear_connector(cf.name)
            self.writer.write_connector(cf)
            self.cache.mark(path)
            ic += 1

        # Utils.js
        utils = self.parser.find_utils()
        if utils and (not incremental or self.cache.changed(utils)):
            self.writer.write_connector_lists(self.parser.parse_utils(utils))
            self.cache.mark(utils)

        # spec files
        for path in self.parser.find_spec_files():
            if incremental and not self.cache.changed(path):
                ss += 1; continue
            sf = self.parser.parse_spec(path)
            if incremental:
                self.writer.clear_spec(sf.name)
            self.writer.write_spec(sf)
            self.cache.mark(path)
            is_ += 1

        self.cache.save()
        print(f"  Connectors : {ic} indexed, {sc} skipped")
        print(f"  Specs      : {is_} indexed, {ss} skipped")
        print(f"  Time       : {time.time()-t0:.2f}s")

    def reindex_files(self, changed_paths: list[str]):
        """
        Partial re-index after a code patch. Only processes the listed files.
        
        Called by the agent right after writing a change:
            indexer.reindex_files([
                "cypress/e2e/configs/Payment/Stripe.js",
                "cypress/e2e/configs/Payment/Utils.js",
            ])
        """
        t0 = time.time()
        for rel in changed_paths:
            path = self.root / rel
            if not path.exists():
                print(f"  WARN {rel} not found"); continue

            if "Utils.js" in rel and "Payment" in rel:
                lists = self.parser.parse_utils(path)
                self.writer.write_connector_lists(lists)
                print(f"  ✓ Utils — {len(lists)} feature lists")

            elif "configs/Payment/" in rel and rel.endswith(".js"):
                cf = self.parser.parse_connector(path)
                self.writer.clear_connector(cf.name)
                self.writer.write_connector(cf)
                print(f"  ✓ {cf.name} — {len(cf.flows)} flows")

            elif "spec/" in rel and rel.endswith(".cy.js"):
                sf = self.parser.parse_spec(path)
                self.writer.clear_spec(sf.name)
                self.writer.write_spec(sf)
                print(f"  ✓ {sf.name} — {len(sf.test_cases)} tests")

            else:
                print(f"  ? {rel} unknown type"); continue

            # Invalidate then re-mark so next incremental run stays consistent
            self.cache.unmark(path)
            self.cache.mark(path)

        self.cache.save()
        print(f"  Done in {(time.time()-t0)*1000:.0f}ms")

    def close(self):
        self.writer.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Hyperswitch cypress indexer")
    ap.add_argument("--repo",     required=True, help="Path to cypress-tests repo root")
    ap.add_argument("--uri",      default="bolt://localhost:7687")
    ap.add_argument("--user",     default="neo4j")
    ap.add_argument("--password", default="Hyperswitch123")
    ap.add_argument("--full",     action="store_true", help="Clean rebuild")
    ap.add_argument("--reindex",  nargs="+", metavar="FILE",
                    help="Re-index specific files (relative to repo root)")
    args = ap.parse_args()

    idx = Indexer(args.repo, args.uri, (args.user, args.password))
    try:
        if args.reindex:
            idx.reindex_files(args.reindex)
        else:
            idx.run(incremental=not args.full)
    finally:
        idx.close()