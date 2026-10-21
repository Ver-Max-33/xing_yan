// The question `ask()` (lib/ask.ts) puts: what goes and what follows, buttons named by what they do (删除分组 / 保留).
import { useSyncExternalStore } from "react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { asking } from "@/lib/ask"

export function Asking() {
  const q = useSyncExternalStore(asking.watch, asking.current)
  return (
    <Dialog open={!!q} onOpenChange={(o) => { if (!o) asking.close(false) }}>
      <DialogContent className="sm:max-w-sm" data-testid="ask">
        <DialogHeader>
          <DialogTitle>{q?.title}</DialogTitle>
          <DialogDescription>{q?.text}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={() => asking.close(false)}>{q?.keep ?? "保留"}</Button>
          <Button variant="destructive" onClick={() => asking.close(true)}>{q?.action}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
