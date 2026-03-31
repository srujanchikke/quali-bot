/**
 * Parses `line_hits.txt` (coverage feedback stdout) into line → hit count.
 * Lines look like: `   127             0  ) -> RouterResponse...`
 */
export function parseLineHitsText(text: string): Map<number, number> {
  const m = new Map<number, number>()
  for (const raw of text.split('\n')) {
    const line = raw.replace(/\r$/, '')
    const match = /^\s*(\d+)\s+(\d+)\s+(.*)$/.exec(line)
    if (match) {
      m.set(Number.parseInt(match[1], 10), Number.parseInt(match[2], 10))
    }
  }
  return m
}
