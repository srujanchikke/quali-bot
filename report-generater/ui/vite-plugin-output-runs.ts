import fs from 'node:fs'
import path from 'node:path'
import type { PreviewServer, ViteDevServer } from 'vite'

/** JSON / text artifacts allowed to be read from a run folder */
const ALLOWED_FILES = new Set([
  'final_report.json',
  'coverage_run_report.json',
  'create_organization.json',
  'path_flow.json',
  'run_summary.txt',
  'line_hits.txt',
])

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
      const isJson = file.endsWith('.json')
      res.setHeader(
        'Content-Type',
        isJson ? 'application/json' : 'text/plain; charset=utf-8',
      )
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
