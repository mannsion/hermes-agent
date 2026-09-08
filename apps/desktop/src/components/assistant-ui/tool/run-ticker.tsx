import { Children, type CSSProperties, isValidElement, type ReactNode } from 'react'

/**
 * A one-line window over a growing list of rows.
 *
 * Each new row slides the one before it up and out, so activity that would
 * otherwise grow down the page reads as a single line ticking over in place.
 * Rows are clipped to a uniform line box so the reel's offset stays exact
 * whatever a row happens to contain.
 *
 * Shared by the tool run (its calls) and a delegation card (each subagent's
 * relayed stream), which are the same thing seen from two sides.
 */
export function ToolRunTicker({ activeIndex, children }: { activeIndex?: number; children: ReactNode }) {
  const rows = Children.toArray(children)
  const visibleIndex = Math.max(0, Math.min(activeIndex ?? rows.length - 1, rows.length - 1))

  return (
    <div className="tool-ticker" data-tool-ticker="">
      <div className="tool-ticker__reel" style={{ '--tool-ticker-index': visibleIndex } as CSSProperties}>
        {rows.map((row, index) => (
          <div
            aria-hidden={index !== visibleIndex ? true : undefined}
            className="tool-ticker__row"
            data-tool-ticker-active={index === visibleIndex ? '' : undefined}
            inert={index !== visibleIndex}
            key={isValidElement(row) ? (row.key ?? index) : index}
          >
            {row}
          </div>
        ))}
      </div>
    </div>
  )
}
