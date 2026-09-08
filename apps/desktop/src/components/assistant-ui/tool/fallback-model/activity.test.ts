import { afterEach, describe, expect, it } from 'vitest'

import { type Locale, setRuntimeI18nLocale, TRANSLATIONS } from '@/i18n'

import { buildToolView, toolActivitySummary, type ToolPart } from './index'

const part = (overrides: Partial<ToolPart>): ToolPart => ({
  type: 'tool-call',
  toolName: 'mcp__game_audio__music_generate',
  ...overrides
})

afterEach(() => setRuntimeI18nLocale('en'))

describe('tool activity presentation', () => {
  it('shows MCP operation, server and bounded request name without arbitrary request fields', () => {
    for (const toolName of ['mcp__game_audio__music_generate', 'mcp__render_farm__render_submit']) {
      const view = buildToolView(
        part({
          toolName,
          args: {
            request: { name: 'Opening theme '.repeat(40), caption: 'PRIVATE CAPTION', lyrics: 'PRIVATE LYRICS' },
            api_key: 'PRIVATE KEY'
          }
        }),
        ''
      )

      expect(view.title).not.toMatch(/mcp|game audio|render farm/i)
      expect(view.serverLabel).toBe(toolName.includes('game_audio') ? 'Game audio' : 'Render farm')
      expect(view.subtitle).toContain('Opening theme')
      expect(view.subtitle.length).toBeLessThan(160)
      expect(view.detail).not.toMatch(/PRIVATE/)
      expect(view.status).toBe('running')
    }
  })

  it('keeps a returned background state distinct from the completed poll and transport errors', () => {
    for (const state of ['running', 'failed']) {
      const result = {
        result: JSON.stringify({
          id: 'job-1',
          name: 'Opening theme',
          state,
          phase: 'planning',
          detail: 'Planning the arrangement',
          error: state === 'failed' ? 'Worker unavailable' : null,
          caption: 'PRIVATE CAPTION'
        })
      }

      const input = part({ toolName: 'mcp__game_audio__music_job', args: { job_id: 'job-1' }, result })
      const before = JSON.stringify(input)
      const view = buildToolView(input, '')
      expect(view.status).toBe('success')
      expect(view.subtitle).toContain(`Reported status: ${state}`)
      expect(view.detail).toContain('Phase: planning')
      expect(view.detail).toContain('Planning the arrangement')
      expect(view.detail).not.toContain('PRIVATE CAPTION')

      if (state === 'failed') {
        expect(view.detail).toContain('Reported error: Worker unavailable')
        expect(view.activitySubtitle).not.toContain('Worker unavailable')
      }

      expect(JSON.stringify(input)).toBe(before)
      expect(buildToolView({ ...input, isError: true }, '').status).toBe('error')
      expect(buildToolView({ ...input, result: result.result }, '').status).toBe('success')
      expect(buildToolView({ ...input, result: { ...result, error: 'Connection refused' } }, '').status).toBe('error')
      expect(buildToolView({ ...input, result: { ...result, isError: true } }, '').status).toBe('error')
    }

    expect(buildToolView(part({ result: { error: 'Connection refused' } }), '').status).toBe('error')

    for (const failure of [
      { isError: true, result: { ok: true, result: { id: 'j', state: 'running' } } },
      { result: { success: true, isError: true, result: { id: 'j', state: 'running' } } },
      { result: { ok: true, error: 'Connection refused', result: { id: 'j', state: 'running' } } }
    ]) {
      expect(buildToolView(part(failure), '').status).toBe('error')
    }
  })

  it('names discovery and skill-reading purposes using their real argument schemas', () => {
    const cases = [
      {
        toolName: 'tool_describe',
        args: { names: ['mcp__game_audio__music_generate', 'mcp__game_audio__music_job'] },
        purpose: 'Reading tool instructions',
        target: 'Music generate'
      },
      {
        toolName: 'tool_search',
        args: { queries: ['create instrumental music'] },
        purpose: 'Finding tools',
        target: 'create instrumental music'
      },
      {
        toolName: 'skill_view',
        args: { name: 'compose-music', file_path: 'references/ace-controls.md' },
        purpose: 'Reading skill',
        target: 'compose-music'
      }
    ]

    for (const { purpose, target, ...input } of cases) {
      const view = buildToolView(part(input), '')
      expect(view.title).toContain(purpose)
      expect([view.title, view.subtitle].join(' ')).toContain(target)
      expect(view.detail).not.toContain('mcp__')
    }
  })

  it('preserves readable ordinary MCP output when expanded without putting it in the activity summary', () => {
    for (const result of [
      { result: 'The rendered file is ready for review.' },
      { result: JSON.stringify([{ title: 'First search match' }, { title: 'Second search match' }]) }
    ]) {
      const input = part({ toolName: 'mcp__research__lookup', args: { name: 'Research request' }, result })
      const view = buildToolView(input, '')
      const expectedOutput = result.result.startsWith('[') ? 'First search match' : result.result

      expect(view.detail).toContain(expectedOutput)
      expect(view.activitySubtitle).not.toContain(expectedOutput)
      expect(toolActivitySummary(input)).not.toContain(expectedOutput)
    }
  })

  it('keeps malformed or oversized MCP payloads out of summaries and bounds the grouped label', () => {
    for (const result of [
      '{broken JSON',
      { result: 'PRIVATE OUTPUT'.repeat(30_000) },
      { result: { result: { result: { result: { name: 'Too deep' } } } } }
    ]) {
      const input = part({ result, args: { request: { name: 'Opening theme '.repeat(100) } } })
      const view = buildToolView(input, '')
      expect(view.activitySubtitle).not.toMatch(/PRIVATE|broken JSON|Too deep/)
      expect(toolActivitySummary(input)).not.toMatch(/PRIVATE|broken JSON|Too deep/)
      expect(toolActivitySummary(input)!.length).toBeLessThanOrEqual(200)
    }

    expect(toolActivitySummary(part({ toolName: 'terminal' }))).toBeUndefined()
    expect(toolActivitySummary(part({ toolName: 'constructor' }))).toBeUndefined()
    expect(toolActivitySummary(part({}), false)).not.toContain('Running')
  })

  it('localizes activity purposes, reported-state labels and payload disclosure labels', () => {
    for (const locale of ['en', 'ar', 'ja', 'ru', 'zh', 'zh-hant'] as Locale[]) {
      setRuntimeI18nLocale(locale)
      const copy = TRANSLATIONS[locale].assistant.tool.activity
      expect(buildToolView(part({ toolName: 'tool_search', args: { queries: 'music' } }), '').title).toBe(
        copy.findTools.pending
      )
      expect(buildToolView(part({ result: { result: { id: 'j', state: 'running' } } }), '').subtitle).toContain(
        copy.reportedStatus('running')
      )

      for (const value of [copy.payload, copy.arguments, copy.result]) {
        expect(value.length).toBeGreaterThan(0)
      }

      if (locale !== 'en') {
        expect(copy.findTools.pending).not.toBe(TRANSLATIONS.en.assistant.tool.activity.findTools.pending)
      }
    }
  })
})
