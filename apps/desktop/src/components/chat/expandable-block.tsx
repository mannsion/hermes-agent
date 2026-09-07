'use client'

import { type ReactNode, useCallback, useId, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { useResizeObserver } from '@/hooks/use-resize-observer'
import { useI18n } from '@/i18n'
import { ChevronDown } from '@/lib/icons'
import { cn } from '@/lib/utils'

interface ExpandableBlockProps {
  children: ReactNode
  className?: string
}

export function ExpandableBlock({ children, className }: ExpandableBlockProps) {
  const { t } = useI18n()
  const viewportId = useId()
  const viewportRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const [expanded, setExpanded] = useState(false)
  const [overflowing, setOverflowing] = useState(false)

  // Read only during ResizeObserver delivery, after layout. Observe intrinsic
  // content as well as the viewport: streamed code keeps growing even when the
  // viewport has reached its cap and no longer emits resize notifications.
  const measure = useCallback(() => {
    const viewport = viewportRef.current

    if (viewport) {
      setOverflowing(viewport.scrollHeight > viewport.clientHeight + 1)
    }
  }, [])

  useResizeObserver(measure, viewportRef, contentRef)

  return (
    <div className="min-w-0">
      <div
        className={cn(
          'scrollbar-overlay overflow-x-auto overflow-y-auto',
          expanded ? 'max-h-none' : 'max-h-96',
          className
        )}
        id={viewportId}
        ref={viewportRef}
      >
        <div className="flow-root" ref={contentRef}>
          {children}
        </div>
      </div>
      {(expanded || overflowing) && (
        // Keep controls below the content, clear of both scrollbars and the
        // final line. Expansion removes the cap entirely and follows streaming.
        <div className="flex justify-end px-2 py-1">
          <Button
            aria-controls={viewportId}
            aria-expanded={expanded}
            onClick={() => setExpanded(value => !value)}
            size="micro"
            type="button"
            variant="ghost"
          >
            {expanded ? t.common.collapse : t.common.expand}
            <ChevronDown className={cn('transition-transform', expanded && 'rotate-180')} />
          </Button>
        </div>
      )}
    </div>
  )
}
