// One hover tip for the whole app: text cut short with an ellipsis (truncate, line-clamp) shows in full, and a title
// shows as this tip instead of the browser's own — which in the desktop app comes after a second or more, not at all
// while the window is not in front, and never over the window's drag areas. Rests 300 ms (as the app's other tooltips),
// at once when moving straight from one to the next; a click, a key, a scroll or the wheel puts it away until the
// pointer moves on. Mounted once (app/App).
import { useEffect, useLayoutEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"

const ID = "wb-hover-tip"
const DELAY = 300            // the app's TooltipProvider delay
const WARM = 400             // moving on within this long after one was shown: the next shows at once
const HIDDEN = "data-hover-title"   // a title while the pointer is on it — away from the browser, which would show it too

interface Tip { anchor: Element; text: string; more: string | null }

/** Whether the element shows less than its text: an ellipsis (text-overflow) or a line clamp that clips it. */
function clipped(el: HTMLElement): boolean {
  if (el.scrollWidth <= el.clientWidth + 1 && el.scrollHeight <= el.clientHeight + 1) return false
  const cs = getComputedStyle(el)
  return cs.textOverflow === "ellipsis" || (cs.webkitLineClamp !== "none" && cs.webkitLineClamp !== "")
}

const titleOf = (el: Element) => el.getAttribute("title") || el.getAttribute(HIDDEN)
const flat = (s: string) => s.replace(/\s+/g, " ")

/** What the pointer rests on: the nearest text cut short and the nearest title around it (a Radix tooltip's trigger
 *  has its own); null when there is neither. */
function tipFor(start: Element | null): (Tip & { titled: Element | null }) | null {
  let cut: HTMLElement | null = null
  let titled: Element | null = null
  for (let el = start; el && el !== document.body; el = el.parentElement) {
    if (!cut && el instanceof HTMLElement && clipped(el)) cut = el
    if (!titled && titleOf(el) && el.getAttribute("data-slot") !== "tooltip-trigger") titled = el
    if (cut && titled) break
  }
  const text = cut ? cut.innerText.trim() : ""
  const title = titled ? titleOf(titled)!.trim() : ""
  if (!text && !title) return null
  const same = !text || !title || flat(text) === flat(title)   // a title that only repeats the text: once, with its lines
  return { anchor: cut ?? titled!, text: same ? title || text : text, more: same ? null : title, titled }
}

export function HoverTips() {
  const [tip, setTip] = useState<Tip | null>(null)
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let timer = 0
    let current: Element | null = null
    let shown = false
    let closedAt = 0
    let hidden: { el: Element; added: string[] } | null = null
    let at = { top: 0, left: 0 }   // where the text was when the pointer came onto it

    // the title goes while the pointer is on it; what it gave screen readers stays: the name of a button that has
    // nothing else to be called by, else the description (the tip)
    const hideTitle = (el: Element) => {
      const title = el.getAttribute("title")
      if (!title) return
      el.setAttribute(HIDDEN, title)
      el.removeAttribute("title")
      const added: string[] = []
      if (!el.hasAttribute("aria-label") && !el.hasAttribute("aria-labelledby") && !el.textContent?.trim()) {
        el.setAttribute("aria-label", title)
        added.push("aria-label")
      } else if (!el.hasAttribute("aria-describedby")) {
        el.setAttribute("aria-describedby", ID)
        added.push("aria-describedby")
      }
      hidden = { el, added }
    }
    const restoreTitle = () => {
      if (!hidden) return
      const { el, added } = hidden
      const title = el.getAttribute(HIDDEN)
      el.removeAttribute(HIDDEN)
      if (title && !el.hasAttribute("title")) el.setAttribute("title", title)   // a title React set meanwhile stays
      for (const a of added) el.removeAttribute(a)
      hidden = null
    }
    const hideTip = () => {
      window.clearTimeout(timer)
      shown = false
      setTip(null)
    }
    const leave = () => {
      if (shown) closedAt = Date.now()
      hideTip()
      restoreTitle()
      current = null
    }
    const over = (e: PointerEvent) => {
      if (e.pointerType === "touch") return
      const found = tipFor(e.target as Element)
      if ((found?.anchor ?? null) === current) return
      leave()
      if (!found) return
      current = found.anchor
      const r = found.anchor.getBoundingClientRect()
      at = { top: r.top, left: r.left }
      if (found.titled) hideTitle(found.titled)
      timer = window.setTimeout(() => {
        if (!found.anchor.isConnected) return
        shown = true
        setTip({ anchor: found.anchor, text: found.text, more: found.more })
      }, Date.now() - closedAt < WARM ? 0 : DELAY)
    }
    const out = (e: PointerEvent) => { if (!e.relatedTarget) leave() }   // out of the window
    // the text scrolled from under the pointer; a scroll told only after the pointer came onto it (the browser tells
    // it a frame late) moved nothing since
    const scrolled = () => {
      const r = current?.getBoundingClientRect()
      if (r && (r.top !== at.top || r.left !== at.left)) hideTip()
    }
    const opts = { capture: true, passive: true }
    document.addEventListener("pointerover", over, opts)
    document.addEventListener("pointerout", out, opts)
    for (const ev of ["pointerdown", "keydown", "wheel"]) document.addEventListener(ev, hideTip, opts)
    window.addEventListener("scroll", scrolled, opts)
    window.addEventListener("blur", hideTip)
    return () => {
      leave()
      document.removeEventListener("pointerover", over, opts)
      document.removeEventListener("pointerout", out, opts)
      for (const ev of ["pointerdown", "keydown", "wheel"]) document.removeEventListener(ev, hideTip, opts)
      window.removeEventListener("scroll", scrolled, opts)
      window.removeEventListener("blur", hideTip)
    }
  }, [])

  // under the text (over it when there is no room below), inside the window; in a list whose next line it would cover
  // (data-tip-side="left": the K 线's 价位梯), beside it on the left when there is room
  useLayoutEffect(() => {
    const el = box.current
    if (!tip || !el) return
    const r = tip.anchor.getBoundingClientRect()
    const { width: w, height: h } = el.getBoundingClientRect()
    const clampY = (y: number) => Math.max(8, Math.min(y, window.innerHeight - h - 8))
    if (tip.anchor.closest('[data-tip-side="left"]') && r.left - 8 - w >= 8) {
      el.style.top = `${clampY(r.top + r.height / 2 - h / 2)}px`
      el.style.left = `${r.left - 8 - w}px`
    } else {
      const below = r.bottom + 6 + h <= window.innerHeight - 8
      el.style.top = `${below ? r.bottom + 6 : Math.max(8, r.top - 6 - h)}px`
      el.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - w - 8))}px`
    }
    el.style.visibility = "visible"
  }, [tip])

  if (!tip) return null
  return createPortal(
    <div ref={box} id={ID} role="tooltip" data-testid="hover-tip" style={{ visibility: "hidden", top: 0, left: 0 }}
      className="pointer-events-none fixed z-[100] w-max max-w-[min(28rem,calc(100vw-16px))] rounded-md bg-foreground px-3 py-1.5 text-[0.75rem] leading-relaxed whitespace-pre-line text-background shadow-md">
      {tip.text}
      {tip.more && <div className="mt-1 text-background/75">{tip.more}</div>}
    </div>,
    document.body,
  )
}
