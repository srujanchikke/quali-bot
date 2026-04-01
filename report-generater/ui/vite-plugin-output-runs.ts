import fs from 'node:fs'
import path from 'node:path'
import type { PreviewServer, ViteDevServer } from 'vite'

/** JSON / text artifacts allowed to be read from a run folder */
const ALLOWED_FILES = new Set([
  'final_report.json',
  'coverage_run_report.json',
  'input.json',
  'create_organization.json',
  'path_flow.json',
  'run_summary.txt',
  'line_hits.txt',
  'terminal_output.log',
  'flow_pipeline.log',
  'router_run.log',
  'cypress_parsed.json',
  'lcov.info',
])

function contentTypeFor(filePath: string): string {
  const ext = path.extname(filePath).toLowerCase()
  switch (ext) {
    case '.html':
      return 'text/html; charset=utf-8'
    case '.css':
      return 'text/css; charset=utf-8'
    case '.js':
      return 'text/javascript; charset=utf-8'
    case '.json':
      return 'application/json'
    case '.svg':
      return 'image/svg+xml'
    case '.png':
      return 'image/png'
    case '.jpg':
    case '.jpeg':
      return 'image/jpeg'
    case '.gif':
      return 'image/gif'
    case '.woff':
      return 'font/woff'
    case '.woff2':
      return 'font/woff2'
    default:
      return 'text/plain; charset=utf-8'
  }
}

function listRuns(outputRoot: string): string[] {
  if (!fs.existsSync(outputRoot)) return []
  const entries = fs.readdirSync(outputRoot, { withFileTypes: true })
  return entries
    .filter((d) => d.isDirectory())
    .filter((d) => {
      const dir = path.join(outputRoot, d.name)
      return (
        fs.existsSync(path.join(dir, 'final_report.json')) ||
        fs.existsSync(path.join(dir, 'coverage_run_report.json'))
      )
    })
    .map((d) => d.name)
    .sort()
    .reverse()
}

function shouldSkipChildren(relativePath: string): boolean {
  return (
    relativePath === 'profraw' ||
    relativePath === 'coverage-html/crates' ||
    relativePath === 'coverage-html/badges'
  )
}

function listRunTree(runRoot: string, prefix = '', relativePath = ''): string[] {
  if (!fs.existsSync(runRoot)) return []
  const entries = fs.readdirSync(runRoot, { withFileTypes: true })
  const sortedEntries = entries.sort((a, b) => a.name.localeCompare(b.name))
  const lines: string[] = []

  sortedEntries.forEach((entry, index) => {
    const isLast = index === sortedEntries.length - 1
    const branch = isLast ? '└── ' : '├── '
    const childPrefix = prefix + (isLast ? '    ' : '│   ')
    const nextRelativePath = relativePath ? `${relativePath}/${entry.name}` : entry.name

    if (entry.isDirectory()) {
      lines.push(`${prefix}${branch}${entry.name}/`)
      if (!shouldSkipChildren(nextRelativePath)) {
        lines.push(...listRunTree(path.join(runRoot, entry.name), childPrefix, nextRelativePath))
      }
    } else {
      lines.push(`${prefix}${branch}${entry.name}`)
    }
  })

  return lines
}

function attachOutputRunsMiddleware(
  middlewares: ViteDevServer['middlewares'],
  outputRoot: string,
) {
  middlewares.use((req, res, next) => {
    const url = req.url?.split('?')[0] ?? ''

    if (url === '/api/runs' && req.method === 'GET') {
      try {
        const runs = listRuns(outputRoot)
        res.setHeader('Content-Type', 'application/json')
        res.end(JSON.stringify({ runs }))
      } catch (e) {
        res.statusCode = 500
        res.setHeader('Content-Type', 'application/json')
        res.end(
          JSON.stringify({
            error: e instanceof Error ? e.message : 'list_failed',
          }),
        )
      }
      return
    }

    const treeMatch = url.match(/^\/api\/run-tree\/([^/]+)$/)
    if (treeMatch && req.method === 'GET') {
      const runId = decodeURIComponent(treeMatch[1])
      if (runId.includes('..')) {
        res.statusCode = 403
        res.end()
        return
      }
      const runRoot = path.join(outputRoot, runId)
      const resolvedOutputRoot = path.resolve(outputRoot)
      const resolvedRunRoot = path.resolve(runRoot)
      if (
        !resolvedRunRoot.startsWith(resolvedOutputRoot + path.sep) &&
        resolvedRunRoot !== resolvedOutputRoot
      ) {
        res.statusCode = 403
        res.end()
        return
      }
      if (!fs.existsSync(resolvedRunRoot) || !fs.statSync(resolvedRunRoot).isDirectory()) {
        res.statusCode = 404
        res.end()
        return
      }
      res.setHeader('Content-Type', 'application/json')
      res.end(JSON.stringify({ files: [`${runId}/`, ...listRunTree(resolvedRunRoot)] }))
      return
    }

    const htmlMatch = url.match(/^\/api\/run-html\/([^/]+)\/(.+)$/)
    if (htmlMatch && req.method === 'GET') {
      const runId = decodeURIComponent(htmlMatch[1])
      const relPath = decodeURIComponent(htmlMatch[2])
      if (
        runId.includes('..') ||
        relPath.includes('..') ||
        relPath.startsWith('/') ||
        relPath.length === 0
      ) {
        res.statusCode = 403
        res.end()
        return
      }
      const filePath = path.join(outputRoot, runId, 'coverage-html', relPath)
      const resolvedRoot = path.resolve(path.join(outputRoot, runId, 'coverage-html'))
      const resolvedFile = path.resolve(filePath)
      if (!resolvedFile.startsWith(resolvedRoot + path.sep) && resolvedFile !== resolvedRoot) {
        res.statusCode = 403
        res.end()
        return
      }
      if (!fs.existsSync(resolvedFile) || !fs.statSync(resolvedFile).isFile()) {
        res.statusCode = 404
        res.end()
        return
      }
      res.setHeader('Content-Type', contentTypeFor(resolvedFile))
      fs.createReadStream(resolvedFile).pipe(res)
      return
    }

    const m = url.match(/^\/api\/run\/([^/]+)\/([^/]+)$/)
    if (m && req.method === 'GET') {
      const runId = decodeURIComponent(m[1])
      const file = decodeURIComponent(m[2])
      if (
        runId.includes('..') ||
        file.includes('..') ||
        file.includes('/') ||
        !ALLOWED_FILES.has(file)
      ) {
        res.statusCode = 403
        res.end()
        return
      }
      const filePath = path.join(outputRoot, runId, file)
      const resolvedRoot = path.resolve(outputRoot)
      const resolvedFile = path.resolve(filePath)
      if (!resolvedFile.startsWith(resolvedRoot + path.sep) && resolvedFile !== resolvedRoot) {
        res.statusCode = 403
        res.end()
        return
      }
      if (!fs.existsSync(resolvedFile) || !fs.statSync(resolvedFile).isFile()) {
        res.statusCode = 404
        res.end()
        return
      }
      res.setHeader('Content-Type', contentTypeFor(resolvedFile))
      fs.createReadStream(resolvedFile).pipe(res)
      return
    }

    next()
  })
}

export function outputRunsPlugin(outputRoot: string) {
  const resolved = path.resolve(outputRoot)
  return {
    name: 'output-runs',
    configureServer(server: ViteDevServer) {
      attachOutputRunsMiddleware(server.middlewares, resolved)
    },
    configurePreviewServer(server: PreviewServer) {
      attachOutputRunsMiddleware(server.middlewares, resolved)
    },
  }
}
