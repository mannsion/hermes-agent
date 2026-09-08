/**
 * Real Electron / gateway reducer / Thread coverage with synthetic tool events.
 * The local mock keeps one real session alive; no MCP or music model is called.
 * Injected rows are in-memory activity, not a claim about persisted job history.
 */

import { type WebSocketRoute } from '@playwright/test'

import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'
import { MOCK_REPLY } from './mock-server'
import { expect, test } from './test'

const PROMPT = 'E2E_TOOL_ACTIVITY: show the progress of my opening theme.'
const CAPTION = 'FIXTURE EXACT CAPTION: acoustic strings and a solo flute.'
const PRIVATE_VALUE = 'FIXTURE PRIVATE VALUE'
const SURFACE = '[data-composer-target]:not([data-pane-hidden] [data-composer-target])'

test('shows live actions, inspectable earlier calls, and the job state reported by a completed music poll', async ({}, testInfo) => {
  test.setTimeout(180_000)
  let fixture: MockBackendFixture | undefined

  try {
    fixture = await setupMockBackend({ mockServer: { holdFirstCompletionContaining: PROMPT } })
    const { app, mock, page } = fixture
    let sessionId = ''
    let activeRoute: WebSocketRoute | undefined

    // Preserve all real RPC traffic. Only the extra tool events below are
    // synthetic; they travel through the normal JSON-RPC event dispatcher.
    await page.routeWebSocket(/\/api\/ws(?:\?|$)/, route => {
      const server = route.connectToServer()

      server.onMessage(message => {
        const frame = JSON.parse(message.toString())

        if (frame.method === 'event' && frame.params?.type === 'message.start') {
          sessionId = frame.params.session_id
          activeRoute = route
        }

        route.send(message)
      })
    })

    // Routing applies to newly opened sockets, so reconnect this sandbox page.
    await page.reload()
    await waitForAppReady(fixture, 120_000)
    const surface = page.locator(SURFACE).last()
    const transcript = surface.locator('[data-slot="aui_thread-viewport"]')
    const composer = surface.locator('[contenteditable="true"]').first()
    await composer.click()
    await composer.type(PROMPT)
    await page.keyboard.press('Enter')
    await mock.waitForHeldCompletion()
    await expect.poll(() => Boolean(sessionId && activeRoute)).toBe(true)
    // Streaming mocks hold after their first word. Let that real delta reach
    // the Thread before adding tools, so it cannot arrive behind the live run.
    await expect(transcript.locator('[data-role="assistant"]')).toContainText(MOCK_REPLY.split(' ')[0])

    const emit = (type: 'tool.start' | 'tool.progress' | 'tool.complete', payload: Record<string, unknown>) => {
      // Unsequenced fixtures do not advance the real backend's replay watermark.
      activeRoute!.send(
        JSON.stringify({
          jsonrpc: '2.0',
          method: 'event',
          params: { type, session_id: sessionId, payload: { ...payload, timestamp: Date.now() / 1000 } }
        })
      )
    }

    const describe = {
      tool_id: 'fixture-describe',
      name: 'tool_describe',
      args: { names: ['mcp__game_audio__music_generate', 'mcp__game_audio__music_job'] }
    }
    emit('tool.start', describe)
    emit('tool.complete', { ...describe, result: { description: PRIVATE_VALUE } })

    const generate = { tool_id: 'fixture-generate', name: 'mcp__game_audio__music_generate' }
    emit('tool.start', { ...generate, args: {} })
    const group = transcript.locator('[data-tool-group]').last()
    const header = group.locator('[data-tool-summary] button[aria-expanded]')
    await expect(header).toContainText('Running music generate')
    const generateArgs = { request: { name: 'Opening theme', caption: CAPTION }, api_key: PRIVATE_VALUE }
    emit('tool.progress', { ...generate, args: generateArgs })
    await expect(header).toContainText('Opening theme')
    await expect(header).toHaveAttribute('aria-expanded', 'false')
    await expect(group.locator('[data-tool-ticker-active] [data-tool-activity]')).toHaveText('Opening theme')
    await expect(transcript).not.toContainText(CAPTION)
    await expect(transcript).not.toContainText(PRIVATE_VALUE)
    await page.screenshot({ path: testInfo.outputPath('tool-activity-live-collapsed.png') })

    // A live group is an actual disclosure: earlier calls can be opened now.
    await header.click()
    await expect(header).toHaveAttribute('aria-expanded', 'true')
    await expect(group.locator('[data-tool-ticker]')).toHaveCount(0)
    await expect(group.locator('[data-tool-row]')).toHaveCount(2)
    await expect(group).toContainText('Read tool instructions')

    emit('tool.complete', {
      ...generate,
      args: generateArgs,
      result: { result: JSON.stringify({ id: 'fixture-music-job', name: 'Opening theme', state: 'queued' }) }
    })
    const poll = {
      tool_id: 'fixture-poll-1',
      name: 'mcp__game_audio__music_job',
      args: { job_id: 'fixture-music-job' }
    }
    emit('tool.start', poll)
    const pollResult = {
      result: JSON.stringify({
        id: 'fixture-music-job',
        name: 'Opening theme',
        state: 'running',
        phase: 'planning',
        detail: 'Planning the arrangement',
        caption: CAPTION
      })
    }
    emit('tool.complete', { ...poll, result: pollResult })
    await expect(group.locator('[data-tool-row]')).toHaveCount(3)
    await expect(header).toHaveAttribute('aria-expanded', 'true')
    await expect(header).toContainText('Reported status: running · Phase: planning')
    const pollRow = group.locator('[data-tool-row]').nth(2)
    await expect(pollRow.locator('button[aria-expanded]').first()).toContainText('Music job')
    await expect(pollRow).not.toContainText('Running music job')
    await expect(pollRow).toContainText('Reported status: running · Phase: planning')
    await expect(pollRow.locator('[aria-label="Running"]')).toHaveCount(0)
    await expect(transcript).not.toContainText(CAPTION)

    await pollRow.locator('button[aria-expanded]').first().click()
    await expect(pollRow).toContainText('Planning the arrangement')
    await expect(pollRow).not.toContainText(CAPTION)
    await pollRow.getByRole('button', { name: 'Input and output', exact: true }).click()
    const payload = pollRow.locator('pre').filter({ hasText: 'Input:' })
    await expect(payload).toContainText('mcp__game_audio__music_job')
    await expect(payload).toContainText('"job_id": "fixture-music-job"')
    await expect(payload).toContainText('Output:')
    await expect(payload).toContainText(CAPTION)
    await page.screenshot({ path: testInfo.outputPath('tool-activity-planning-wide.png') })

    // New calls must not collapse a history/payload the user chose to inspect.
    const nextPoll = { ...poll, tool_id: 'fixture-poll-2' }
    emit('tool.start', nextPoll)
    await expect(group.locator('[data-tool-row]')).toHaveCount(4)
    await expect(header).toHaveAttribute('aria-expanded', 'true')
    await expect(payload).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath('tool-activity-history-wide.png') })

    await app.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows()[0]
      window.unmaximize()
      window.setMinimumSize(640, 600)
      window.setSize(720, 800)
    })
    await expect.poll(() => page.evaluate(() => window.innerWidth)).toBeLessThanOrEqual(720)
    await expect(header).toHaveAttribute('aria-expanded', 'true')
    await expect(payload).toBeVisible()
    expect(await transcript.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath('tool-activity-history-narrow.png') })

    // The fresh sandbox follows the OS theme. Exercise its dark rendering
    // through the supported media preference, preserving the same live state.
    await page.emulateMedia({ colorScheme: 'dark' })
    await expect(page.locator('html')).toHaveClass(/dark/)
    await payload.evaluate(node => {
      node.scrollTop = node.scrollHeight
    })
    await expect(payload).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath('tool-activity-output-narrow-dark.png') })

    await header.click()
    await expect(header).toHaveAttribute('aria-expanded', 'false')
    await expect(transcript).not.toContainText(CAPTION)
    await expect(transcript).not.toContainText(PRIVATE_VALUE)
    await expect(group.locator('[data-tool-ticker-active]')).toContainText('Running music job')
  } finally {
    fixture?.mock.releaseHeldStream()
    await fixture?.cleanup()
  }
})
