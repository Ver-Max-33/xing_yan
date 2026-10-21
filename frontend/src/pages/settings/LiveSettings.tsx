// 设置 → 盘中行情 (spec §4.8): the prod JP source's session. Tachibana's login is only ever your click: during the
// transition its phone authentication must come first, within 3 minutes. The session serves until the night close.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { api } from "@/lib/api"

interface TachibanaStatus { configured: boolean; base: string; demo: boolean; logged_in: boolean; day: string | null }

export function TachibanaLogin() {
  const qc = useQueryClient()
  const { data } = useQuery({ queryKey: ["live", "tachibana"], queryFn: () => api<TachibanaStatus>("/live/tachibana") })
  const login = useMutation({
    mutationFn: () => api<TachibanaStatus>("/live/tachibana/login", { method: "POST" }),
    onSuccess: (s) => { qc.setQueryData(["live", "tachibana"], s); toast.success("立花证券已登录：今天的会话到夜间闭局为止") },
    onError: (e) => toast.error(String(e)),
  })
  const where = data?.demo ? "演示环境" : "正式环境"
  return (
    <div className="flex min-h-12 items-center gap-3 py-2 text-[0.875rem]" data-testid="tachibana-login">
      <div className="w-32 font-medium">立花 登录</div>
      <div className="flex-1 text-muted-foreground">
        {!data ? "…" : !data.configured ? "先配置上面的认证 ID 和私钥文件" : data.logged_in ? `今天已登录 · ${where}` : `今天还没登录 · ${where}`}
        <span className="text-subtle-foreground"> · 过渡期内先打 API 电话认证，3 分钟内点登录；会话到夜间闭局为止</span>
      </div>
      <Button size="sm" variant="outline" disabled={!data?.configured || login.isPending} onClick={() => login.mutate()}>登录立花证券</Button>
    </div>
  )
}
