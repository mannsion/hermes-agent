import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { ExpandableBlock } from './expandable-block'

// Deliver only the node whose size changed. The browser does not report a
// capped viewport when streamed content grows inside it.
const observed = new Map<Element, ResizeObserverCallback>()

class TestResizeObserver {
  constructor(private readonly callback: ResizeObserverCallback) {}
  observe(target: Element) {
    observed.set(target, this.callback)
  }
  unobserve(target: Element) {
    observed.delete(target)
  }
  disconnect() {
    observed.clear()
  }
}

beforeAll(() => vi.stubGlobal('ResizeObserver', TestResizeObserver))
afterEach(cleanup)

function resize(target: Element) {
  act(() => {
    observed.get(target)?.([{ target } as ResizeObserverEntry], {} as ResizeObserver)
  })
}

function dimensions(element: Element, visible: number, total: number) {
  Object.defineProperties(element, {
    clientHeight: { configurable: true, value: visible },
    scrollHeight: { configurable: true, value: total }
  })
}

describe('ExpandableBlock', () => {
  it('offers expansion when streamed content grows inside an unchanged viewport', () => {
    const view = render(
      <ExpandableBlock>
        <pre>short code</pre>
      </ExpandableBlock>
    )

    const viewport = view.container.querySelector('.scrollbar-overlay')!
    dimensions(viewport, 120, 120)
    resize(viewport)
    expect(screen.queryByRole('button', { name: 'Expand' })).toBeNull()

    view.rerender(
      <ExpandableBlock>
        <pre>{'const x = 1\n'.repeat(80)}</pre>
      </ExpandableBlock>
    )
    dimensions(viewport, 120, 1280)
    resize(viewport.firstElementChild!)
    expect(screen.getByRole('button', { name: 'Expand' }).getAttribute('aria-expanded')).toBe('false')

    view.rerender(
      <ExpandableBlock>
        <pre>short code again</pre>
      </ExpandableBlock>
    )
    dimensions(viewport, 32, 32)
    resize(viewport.firstElementChild!)
    expect(screen.queryByRole('button', { name: 'Expand' })).toBeNull()
  })

  it('keeps expansion through streaming updates and leaves controls outside the scrollable code', () => {
    const view = render(
      <ExpandableBlock>
        <pre>{'line\n'.repeat(80)}</pre>
      </ExpandableBlock>
    )

    const viewport = view.container.querySelector('.scrollbar-overlay')!
    dimensions(viewport, 384, 1280)
    resize(viewport)
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }))

    view.rerender(
      <ExpandableBlock>
        <pre>{'line\n'.repeat(100)}</pre>
      </ExpandableBlock>
    )
    dimensions(viewport, 1600, 1600)
    resize(viewport.firstElementChild!)
    const collapse = screen.getByRole('button', { name: 'Collapse' })
    expect(collapse.getAttribute('aria-expanded')).toBe('true')
    expect(viewport.contains(collapse)).toBe(false)
    expect(viewport.className).toContain('overflow-x-auto')

    fireEvent.click(collapse)
    dimensions(viewport, 384, 1600)
    resize(viewport)
    expect(screen.getByRole('button', { name: 'Expand' }).getAttribute('aria-expanded')).toBe('false')
  })
})
