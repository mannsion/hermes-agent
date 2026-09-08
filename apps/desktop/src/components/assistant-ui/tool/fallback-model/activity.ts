import { translateNow } from '@/i18n'
import { capitalize, firstStringField } from '@/lib/text'

import { compactPreview, isRecord, parseMaybeObject } from './format'
import type { ToolPart } from './types'

type ActivityInput = Pick<ToolPart, 'toolName' | 'args' | 'result' | 'isError'>

interface ToolActivity {
  title: string
  subtitle: string
  detail: string
  serverLabel?: string
  /** Domain state is information returned by a call, not the call's status. */
  reportedResult?: Record<string, unknown>
}

function humanName(name: string): string {
  return compactPreview(capitalize(name.replace(/[_\-.]+/g, ' ')), 72)
}

function mcpName(name: string): { server: string; tool: string } | undefined {
  const match = /^mcp__([A-Za-z0-9_]+?)__([A-Za-z0-9_]+)$/.exec(name)

  return match ? { server: humanName(match[1]), tool: humanName(match[2]) } : undefined
}

/** Only the documented MCP result wrapper is traversed, with a fixed budget.
 * Never stringify arbitrary objects into a product summary. */
function mcpResult(value: unknown): Record<string, unknown> {
  for (let depth = 0; depth < 4; depth += 1) {
    if (typeof value === 'string' && value.length > 262_144) {
      return {}
    }

    const record = parseMaybeObject(value)

    if (record.result == null) {
      return record
    }

    value = record.result
  }

  return {}
}

function shortField(record: Record<string, unknown>, key: string, max = 96): string {
  return typeof record[key] === 'string' ? compactPreview(record[key], max) : ''
}

function shortList(value: unknown, tools = false): string {
  const values = typeof value === 'string' ? [value] : Array.isArray(value) ? value : []

  const labels = values
    .slice(0, 3)
    .filter((item): item is string => typeof item === 'string')
    .map(item => (tools ? mcpName(item)?.tool || humanName(item) : compactPreview(item, 72)))

  return compactPreview([...labels, ...(values.length > 3 ? ['…'] : [])].join(', '), 120)
}

const DISCOVERY_PURPOSES: Record<string, string> = {
  tool_describe: 'toolInstructions',
  tool_search: 'findTools',
  skill_view: 'readSkill'
}

/** Presentation only: whitelist short user-facing fields; raw request/result
 * remain available through the expanded input/output disclosure. */
export function buildToolActivity(tool: ActivityInput, live = tool.result === undefined): ToolActivity | undefined {
  const args = parseMaybeObject(tool.args)
  const purpose = Object.hasOwn(DISCOVERY_PURPOSES, tool.toolName) ? DISCOVERY_PURPOSES[tool.toolName] : undefined

  if (purpose) {
    const targets: Record<string, () => string> = {
      tool_describe: () => shortList(args.names, true),
      tool_search: () => shortList(args.queries),
      skill_view: () => [shortField(args, 'name'), shortField(args, 'file_path')].filter(Boolean).join(' / ')
    }

    const target = compactPreview(targets[tool.toolName](), 120)

    return {
      title: translateNow(`assistant.tool.activity.${purpose}.${live ? 'pending' : 'done'}`),
      subtitle: target,
      detail: target
    }
  }

  const name = mcpName(tool.toolName)

  if (!name) {
    return undefined
  }

  const result = mcpResult(tool.result)
  const request = isRecord(args.request) ? args.request : args
  const requestedName = shortField(request, 'name')
  const returnedName = shortField(result, 'name')
  const state = shortField(result, 'state', 40)
  const phase = shortField(result, 'phase', 64)
  const detail = shortField(result, 'detail', 240)
  const reportedResult = state && firstStringField(result, ['id', 'job_id']) ? result : undefined

  const reportedError = reportedResult
    ? isRecord(result.error)
      ? shortField(result.error, 'message', 240)
      : shortField(result, 'error', 240)
    : ''

  const reportedStatus = state ? translateNow('assistant.tool.activity.reportedStatus', state) : ''
  const reportedPhase = phase ? translateNow('assistant.tool.activity.reportedPhase', phase) : ''
  const target = returnedName || requestedName

  const details = [
    requestedName && translateNow('assistant.tool.activity.requestName', requestedName),
    returnedName &&
      returnedName !== requestedName &&
      translateNow('assistant.tool.activity.returnedName', returnedName),
    reportedStatus,
    reportedPhase,
    reportedError && translateNow('assistant.tool.activity.reportedError', reportedError),
    detail
  ].filter(Boolean)

  return {
    title: live ? translateNow('assistant.tool.titleTemplates.runningTool', name.tool) : name.tool,
    serverLabel: name.server,
    subtitle: [target, reportedStatus, reportedPhase].filter(Boolean).join(' · '),
    detail: details.join('\n'),
    reportedResult
  }
}

/** Current purpose for compact grouped headers. Callers can explicitly settle
 * an interrupted run whose last tool never received a result. */
export function toolActivitySummary(tool: ActivityInput, live = tool.result === undefined): string | undefined {
  const activity = buildToolActivity(tool, live)

  return activity ? compactPreview([activity.title, activity.subtitle].filter(Boolean).join(' · '), 200) : undefined
}
