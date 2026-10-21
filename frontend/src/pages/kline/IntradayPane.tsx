// The K-line page's 分时 (spec §8.5): the stock's kept minutes in place of the K-line, the days side by side as 盯盘
// shows them, with the daily box's top and the 分时小箱 (design 2026-10-12-structures-every-period: 1 / 5 / 15
// minutes, the same choice and the same boxes as 盯盘's); 盯盘 keeps 波段量 and the alerts.
import { Link } from "react-router"
import { Button } from "@/components/ui/button"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import type { Market } from "@/lib/api"
import { shortDate } from "@/lib/format"
import { type MicroPeriod, type MinuteDays, useMicro, useMinutes } from "@/lib/live"
import { usePersisted } from "@/lib/usePersisted"
import { IntradayChart } from "../live/IntradayChart"

const DAYS: MinuteDays[] = [1, 5, 10, 20]
const MICRO: [MicroPeriod | null, string][] = [[1, "1 分"], [5, "5 分"], [15, "15 分"], [null, "关"]]

/** `top`: the daily box's top nearest above the price (its 价位梯's), as 盯盘's 分时 draws its stock's. */
export function IntradayPane({ listingId, market, watched, onWatch, top = null }: {
  listingId: string; market: Market; watched: boolean; onWatch: () => void; top?: number | null
}) {
  const [kept, setDays] = usePersisted<MinuteDays>("live.days", 1)   // the same choice as 盯盘's
  const days = DAYS.includes(kept) ? kept : 1
  const { data, error } = useMinutes(listingId, days)
  const [period, setPeriod] = usePersisted<MicroPeriod | null>("live.micro", 5)   // 盯盘's 分时小箱, the same choice
  const { data: micro } = useMicro(listingId, period)
  return (
    <div className="absolute inset-0 z-20 overflow-y-auto bg-background px-4 py-3" data-testid="kline-intraday">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.75rem] text-muted-foreground">
        <span>分时 · 1 分钟{data?.day ? ` · ${shortDate(data.day)}` : ""}</span>
        <ToggleGroup type="single" size="sm" variant="outline" aria-label="几天" value={String(days)}
          onValueChange={(v) => { if (v) setDays(Number(v) as MinuteDays) }}>
          {DAYS.map((d) => <ToggleGroupItem key={d} value={String(d)} className="h-6 px-2 text-[0.75rem]">{d === 1 ? "当天" : `${d} 日`}</ToggleGroupItem>)}
        </ToggleGroup>
        <span>分时小箱</span>
        <ToggleGroup type="single" size="sm" variant="outline" aria-label="分时小箱" value={String(period)}
          onValueChange={(v) => { if (v) setPeriod(v === "null" ? null : Number(v) as MicroPeriod) }}>
          {MICRO.map(([p, l]) => <ToggleGroupItem key={String(p)} value={String(p)} className="h-6 px-2 text-[0.75rem]">{l}</ToggleGroupItem>)}
        </ToggleGroup>
        <span className="flex-1" />
        <Link to={`/live?lid=${encodeURIComponent(listingId)}`} className="hover:text-foreground">波段量和提醒在盯盘页 →</Link>
      </div>
      {data?.minutes.length
        ? <div className="mx-auto mt-2 max-w-[64rem]"><IntradayChart data={data} market={market} top={top} micro={period !== null ? micro : undefined} /></div>
        : <div className="py-16 text-center text-[0.8125rem] text-subtle-foreground" data-testid="kline-intraday-empty">
          {error ? `读不出分时数据：${String(error)}` : !data ? "读取中…" : <>
            {market === "CN"
              ? <p>还没有这只股票的分钟线：开盘期间打开过的 A 股都有（打开后几秒内取到），盯盘的股票（观察分组里的、画了提醒线的）每次取盘中行情时一起记（保留最近 20 个交易日）。</p>
              : <p>还没有这只股票的分钟线：盘中只记下盯盘的股票（观察分组里的、画了提醒线的）和开盘期间打开过的，每次取盘中行情时一起记（保留最近 20 个交易日）。</p>}
            {!watched && market !== "CN" && <Button size="sm" variant="outline" className="mt-3" onClick={onWatch}>加入观察（W），开盘后开始记</Button>}
          </>}
        </div>}
    </div>
  )
}
