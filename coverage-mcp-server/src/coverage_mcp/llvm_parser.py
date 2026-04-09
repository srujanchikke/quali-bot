"""
Parse LLVM coverage JSON format (produced by llvm-cov export).

Structure:
  {
    "data": [{
      "files": [
        {
          "filename": "/path/to/file.rs",
          "summary": {
            "lines":     {"count": N, "covered": N, "percent": N},
            "functions": {"count": N, "covered": N, "percent": N},
            "regions":   {"count": N, "covered": N, "notcovered": N, "percent": N}
          }
        }, ...
      ],
      "functions": [
        {
          "name": "<mangled-symbol>",
          "count": N,          # 0 = never called, >0 = called
          "filenames": ["..."]
        }, ...
      ],
      "totals": {
        "lines":     {"count": N, "covered": N, "percent": N},
        "functions": {"count": N, "covered": N, "percent": N},
        "regions":   {"count": N, "notcovered": N, "percent": N}
      }
    }],
    "type": "llvm.coverage.json.export",
    "version": "2.0.1"
  }
"""

from dataclasses import dataclass, field


@dataclass
class LLVMStat:
    count: int
    covered: int
    percent: float

    def as_dict(self) -> dict:
        missed = self.count - self.covered
        return {
            "count": self.count,
            "covered": self.covered,
            "missed": missed,
            "percent": round(self.percent, 2),
        }


@dataclass
class LLVMFileCoverage:
    filename: str
    lines: LLVMStat
    functions: LLVMStat
    regions: LLVMStat


@dataclass
class FunctionInfo:
    name: str
    count: int          # 0 = untested, >0 = tested
    filenames: list[str]


@dataclass
class LLVMCoverageReport:
    tag: str
    lines: LLVMStat
    functions: LLVMStat
    regions: LLVMStat
    files: list[LLVMFileCoverage] = field(default_factory=list)
    # name -> FunctionInfo; built lazily on first use
    _function_index: list[FunctionInfo] = field(default_factory=list)


def _stat(summary_block: dict, key: str) -> LLVMStat:
    b = summary_block.get(key, {})
    count   = b.get("count", 0)
    covered = b.get("covered", 0)
    # regions use 'notcovered' instead of computing from count-covered
    if "notcovered" in b and covered == 0:
        covered = count - b["notcovered"]
    pct = b.get("percent", 0.0)
    return LLVMStat(count=count, covered=covered, percent=pct)


def parse_llvm_json(data: dict, tag: str) -> LLVMCoverageReport:
    block = data["data"][0]
    totals = block.get("totals", {})

    report = LLVMCoverageReport(
        tag=tag,
        lines=_stat(totals, "lines"),
        functions=_stat(totals, "functions"),
        regions=_stat(totals, "regions"),
    )

    for f in block.get("files", []):
        summary = f.get("summary", {})
        report.files.append(LLVMFileCoverage(
            filename=f["filename"],
            lines=_stat(summary, "lines"),
            functions=_stat(summary, "functions"),
            regions=_stat(summary, "regions"),
        ))

    for fn in block.get("functions", []):
        report._function_index.append(FunctionInfo(
            name=fn.get("name", ""),
            count=fn.get("count", 0),
            filenames=fn.get("filenames", []),
        ))

    return report


def is_llvm_format(data: dict) -> bool:
    """Return True if data looks like an LLVM coverage JSON export."""
    return (
        isinstance(data, dict)
        and "data" in data
        and isinstance(data["data"], list)
        and len(data["data"]) > 0
        and "files" in data["data"][0]
        and "totals" in data["data"][0]
    )
