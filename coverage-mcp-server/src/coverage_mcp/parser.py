"""
Dispatch between two coverage JSON formats:

1. Custom tree format (linesCovered at root, children tree).
   No function/region data.

2. LLVM coverage JSON (data[0].files + data[0].functions + data[0].totals).
   Has line, function, and region coverage.

Use parse_json() for the tree format (existing tools).
Use llvm_parser.parse_llvm_json() / llvm_parser.is_llvm_format() for LLVM format.
"""

from dataclasses import dataclass, field


@dataclass
class LineStat:
    covered: int
    missed: int
    total: int
    percent: float

    def as_dict(self) -> dict:
        return {
            "covered": self.covered,
            "missed": self.missed,
            "total": self.total,
            "percent": round(self.percent, 2),
        }


@dataclass
class FileCoverage:
    filename: str       # full path reconstructed from tree traversal
    lines: LineStat
    uncovered_lines: list[int] = field(default_factory=list)  # 1-based line numbers

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "lines": self.lines.as_dict(),
        }


@dataclass
class CoverageReport:
    tag: str
    totals: LineStat
    files: list[FileCoverage] = field(default_factory=list)


def parse_json(data: dict, tag: str) -> CoverageReport:
    totals = LineStat(
        covered=data.get("linesCovered", 0),
        missed=data.get("linesMissed", 0),
        total=data.get("linesTotal", 0),
        percent=data.get("coveragePercent", 0.0),
    )

    files: list[FileCoverage] = []
    _walk(data.get("children", {}), prefix="", files=files)

    return CoverageReport(tag=tag, totals=totals, files=files)


def _walk(children: dict, prefix: str, files: list[FileCoverage]) -> None:
    for name, node in children.items():
        path = f"{prefix}/{name}" if prefix else name
        if "coverage" in node:
            # Leaf = file
            cov_array: list[int] = node["coverage"]
            uncovered = [
                i + 1  # 1-based line number
                for i, v in enumerate(cov_array)
                if v == 0
            ]
            files.append(FileCoverage(
                filename=path,
                lines=LineStat(
                    covered=node.get("linesCovered", 0),
                    missed=node.get("linesMissed", 0),
                    total=node.get("linesTotal", 0),
                    percent=node.get("coveragePercent", 0.0),
                ),
                uncovered_lines=uncovered,
            ))
        else:
            # Intermediate directory — recurse
            _walk(node.get("children", {}), path, files)
