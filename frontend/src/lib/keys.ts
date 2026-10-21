// The pages' one-letter shortcuts (J / K, ↵, W, E, Space …) and what has focus: a text field, a dialog or a menu keeps
// every key; a focused button, link, switch or tab keeps its own Enter and Space — they press it (Enter on 「对」 had
// opened another hit, Space on any K 线 button had toggled the replay). A list's own row (data-row) is the exception:
// its Enter is the page's — open what it shows.
const TYPING = "input, textarea, select, [contenteditable], [role=dialog], [role=menu]"
const PRESSABLE = "button, a[href], summary, [role=button], [role=switch], [role=checkbox], [role=radio], [role=tab], [role=menuitem], [role=option]"

/** Whether a page's shortcut handler should leave this key alone. */
export function leaveToFocus(e: Pick<KeyboardEvent, "key" | "target" | "metaKey" | "ctrlKey" | "altKey">): boolean {
  const t = e.target instanceof Element ? e.target : null   // a key sent to window has no element
  if (e.metaKey || e.ctrlKey || e.altKey || t?.closest(TYPING)) return true
  if (e.key !== "Enter" && e.key !== " ") return false
  const control = t?.closest(PRESSABLE)
  return !!control && !control.hasAttribute("data-row")
}
