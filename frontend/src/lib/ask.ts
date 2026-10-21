// One way to ask before something cannot be taken back (the dialog is components/Ask.tsx, mounted once): `ask()` waits
// for the answer like window.confirm did — the app's own dialog, not the system's OK / Cancel box.
export interface Question { title: string; text: string; action: string; keep?: string }
type Open = Question & { answer: (yes: boolean) => void }

let open: Open | null = null
const watchers = new Set<() => void>()
const changed = () => watchers.forEach((w) => w())

/** Asks; true when the action was chosen. A question asked while another is open answers that one "no". */
export function ask(q: Question): Promise<boolean> {
  return new Promise((answer) => {
    open?.answer(false)
    open = { ...q, answer }
    changed()
  })
}

export const asking = {
  watch: (w: () => void) => { watchers.add(w); return () => void watchers.delete(w) },
  current: () => open,
  close: (yes: boolean) => {
    const was = open
    open = null
    changed()
    was?.answer(yes)
  },
}
