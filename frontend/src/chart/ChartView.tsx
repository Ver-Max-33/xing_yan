import { useEffect, useRef } from "react"
import { ChartController, type LoadInfo, type Panes, type View } from "./controller"

interface Props {
  view: View | null
  panes: Panes
  drawingsVisible: boolean
  onReady?: (c: ChartController) => void
  onLoad?: (info: LoadInfo) => void
  onError?: (e: unknown) => void
}

/** React wrapper: one ChartController per mounted element. */
export function ChartView({ view, panes, drawingsVisible, onReady, onLoad, onError }: Props) {
  const el = useRef<HTMLDivElement>(null)
  const ctl = useRef<ChartController | null>(null)
  const cb = useRef({ onLoad, onError })
  cb.current = { onLoad, onError }

  useEffect(() => {
    const c = new ChartController(el.current!)
    c.onLoad = (i) => cb.current.onLoad?.(i)
    c.onError = (e) => cb.current.onError?.(e)
    ctl.current = c
    onReady?.(c)
    const ro = new ResizeObserver(() => c.resize())
    ro.observe(el.current!)
    const onTheme = () => c.refreshTheme()
    window.addEventListener("wb-theme", onTheme)
    return () => {
      ro.disconnect()
      window.removeEventListener("wb-theme", onTheme)
      c.dispose()
      ctl.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (view) ctl.current?.open(view)
  }, [view])

  useEffect(() => {
    ctl.current?.applyPanes(panes)
  }, [panes])

  useEffect(() => {
    ctl.current?.setDrawingsVisible(drawingsVisible)
  }, [drawingsVisible])

  return <div ref={el} className="h-full w-full" data-testid="kline-chart" />
}
