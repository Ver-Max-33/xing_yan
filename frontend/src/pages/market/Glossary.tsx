// 「指标说明」(designs 2026-10-12-market-live §7, -market-page-redesign §3.3, §6.3, §7): every number's definition, the
// phase rules with their thresholds and where they come from, what the data says about phases, the strength tags'
// thresholds, why the industries are 申万's, where the numbers come from and how often.
import { CircleHelp } from "lucide-react"
import { Fragment } from "react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"

const DEFS: [string, string][] = [
  ["涨停", "最新价等于交易所的涨停价（盘中就是此刻封着）。只算今天成交过的；新股上市前几天不设涨跌幅，不算。"],
  ["炸板", "今天最高价碰过涨停价，此刻不在涨停。"],
  ["封板率", "涨停 ÷（涨停 + 炸板）。"],
  ["晋级率", "昨天收盘涨停的股票里此刻涨停的比例；昨天涨停不到 10 只时不算。"],
  ["连板", "到上一根 K 线连续涨停 k 天、今天此刻涨停的是 k+1 板（中间停牌不断板，和规则的 LIMIT_UP_STREAK 一样）。"],
  ["最高板", "今天碰过涨停的股票里板数最高的，标封住或炸板。"],
  ["跌停", "最新价等于交易所的跌停价。"],
  ["今天的读数", "和昨天同一分钟比六项：涨停、炸板、封板率、晋级率、跌停、最高板。数量差 3 只以上且超过昨天的 15%、比率差 5 个点以上才算一票；好的比差的多 2 票以上偏强，少 2 票以上偏弱。9:45 以前不比。"],
  ["今天（行业）", "行业里今天成交过的股票涨跌幅的中位数。"],
  ["比大盘", "行业里每只股票涨幅的中位数，减去全市场的中位数：近 5 日（近 5 根 K 线，含今天）、之前 15 日（再往前的第 6 到第 20 根）、近 20 日。按每只股票自己的 K 线数（和规则的 INDUSTRY_RANK 一样），今天的涨跌并进去。"],
  ["强弱标签", "只看近 5 日和之前 15 日（两段不重叠）：近 5 日明显强于大盘、之前 15 日也明显强是一直强，之前没有是转强；近 5 日明显弱于大盘、之前 15 日也明显弱是一直弱，之前没有是转弱；近 5 日和大盘差不多不标。成分 3 只以下的行业不标。"],
  ["名次", "成分超过 3 只的行业按今天涨跌排的名次，如“第 3 / 127”。"],
  ["热力格的颜色", "格子里所选的数，绝对值最大的那个算满格，0–25%、25–50%、50–75%、75–100% 各一档，涨四档红、跌四档绿，0.00% 是灰的。跌的日子里，跌得少的和跌得多的也分得出。"],
  ["成交额倍数", "行业的成交额是这些股票近 5 日平均的几倍；盘中按成交节奏折算到此刻，10:30 以后才有（更早的折算偏差大）。"],
]
const TREND: [string, string, string, string][] = [
  ["申万一级（31 个）", "1.9% / 3.2%", "±1.5% / ±2.5%", "一直强 4.7%、转强 15.1%、转弱 15.5%、一直弱 2.2%"],
  ["申万二级（127 个）", "2.6% / 4.5%", "±2% / ±3.5%", "一直强 4.5%、转强 13.9%、转弱 14.1%、一直弱 2.5%"],
  ["东证 33 業種", "1.3% / 2.4%", "±1% / ±2%", "一直强 5.6%、转强 12.3%、转弱 11.3%、一直弱 2.8%"],
]
const SHENWAN: [string, string, string][] = [
  ["贵州茅台", "白酒", "食品饮料 · 白酒"], ["宁德时代", "电气设备", "电力设备 · 电池"], ["立讯精密", "元器件", "电子 · 消费电子"],
  ["中芯国际", "半导体", "电子 · 半导体"], ["光线传媒", "影视音像", "传媒 · 影视院线"], ["中信证券", "证券", "非银金融 · 证券"], ["工商银行", "银行", "银行 · 国有大型银行"],
]
const PHASES: [string, string][] = [
  ["冰点（急跌）", "当天跌停 ≥ 50 且是涨停的两倍以上；之前在主升或高潮的叫退潮。当天就换。"],
  ["高潮", "二板以上 ≥ 62，或首板 ≥ 218。"],
  ["主升", "最高板 ≥ 8、二板以上 ≥ 19、晋级率 ≥ 27%；或晋级率 ≥ 34.6% 且最高板 ≥ 7、二板以上 ≥ 16。"],
  ["冰点", "最高板 ≤ 4、二板以上 ≤ 8、首板 ≤ 26。"],
  ["退潮", "晋级率 < 18.1% 且二板以上比 5 天前少、5 天前在高处；或晋级率 < 15.4% 且封板率 < 56.5%；或跌停（平滑后）≥ 39 且晋级率 < 24.6%。"],
  ["启动", "晋级率 ≥ 20.4%，且二板以上比 5 天前多 3 家以上（至少 8 家），或最高板比 5 天前高（至少 5 板）。"],
  ["平稳 / 修复", "都不是；刚经过冰点或退潮的叫修复。"],
]

export function Glossary() {
  return (
    <Dialog>
      <DialogTrigger asChild><Button variant="ghost" size="sm" className="h-7 gap-1.5 text-[0.8125rem]"><CircleHelp className="size-3.5" />指标说明</Button></DialogTrigger>
      <DialogContent className="max-h-[85vh] max-w-[44rem] overflow-y-auto">
        <DialogHeader><DialogTitle>市场页的数</DialogTitle></DialogHeader>
        <div className="flex flex-col gap-5 text-[0.8125rem]">
          <dl className="grid grid-cols-[6rem_1fr] gap-x-4 gap-y-2">{DEFS.map(([k, v]) => <Fragment key={k}><dt className="text-muted-foreground">{k}</dt><dd>{v}</dd></Fragment>)}</dl>
          <div>
            <h3 className="mb-1.5 font-semibold">情绪阶段（按收盘算，一天一个）</h3>
            <p className="mb-2 text-muted-foreground">前五项先平滑（指数平均，约 5 天），从上往下判，第一个满足的就是当天的标签；新标签连续两天出现才换（急跌除外）；上证在下降时，启动、主升、高潮一律算平稳。数都是 2020-08-24 到 2026-10-09 这 1,483 个交易日的分位数。</p>
            <dl className="grid grid-cols-[6rem_1fr] gap-x-4 gap-y-1.5">{PHASES.map(([k, v]) => <Fragment key={k}><dt className="text-muted-foreground">{k}</dt><dd>{v}</dd></Fragment>)}</dl>
            <p className="mt-2 text-muted-foreground">阶段只描述现在：同样这 1,483 天，各阶段里当天涨停的股票第二天平均涨 1.9%–2.1%（高潮 2.5%，只有 16 天），差别都在误差之内。阶段说的是现在热不热，不是明天会怎样。</p>
            <p className="mt-2 text-muted-foreground">和你的规则一起看：箱体的 11 条越线买模板（2020-08 起，按交易计划买卖），冰点那天越线的最差——10 条比各自的平均差（形末量尽 −2.4%、下品 · 大箱突破 −1.9%），3 条比同一天随机买的也明显差；启动、主升的日子多数和随机买差不多；高潮只有十五六天，看不出来。越线后收盘站上突破线的比例，主升、高潮、退潮的日子略高（平均 24%–26%，其余 22%–23%）。阶段仍不是买卖信号，但冰点那天的越线更容易收不住。</p>
          </div>
          <div>
            <h3 className="mb-1.5 font-semibold">强弱标签的门槛</h3>
            <p className="mb-2 text-muted-foreground">"明显"按各级行业平时差多少定：平时差距是 2025-09 到 2026-10-09 这 250 个交易日里，每天各行业比大盘多涨少涨的标准差的中位数；门槛取它的四分之三左右。平常一天，31 个一级里十来个带标签。</p>
            <table className="w-full text-left text-[0.75rem]">
              <thead className="text-muted-foreground"><tr><th className="pr-3 font-normal">行业</th><th className="pr-3 font-normal">平时差距（近 5 日 / 之前 15 日）</th><th className="pr-3 font-normal">门槛</th><th className="font-normal">一年里各标签的比例</th></tr></thead>
              <tbody>{TREND.map((r) => <tr key={r[0]}>{r.map((c, i) => <td key={i} className="num py-0.5 pr-3">{c}</td>)}</tr>)}</tbody>
            </table>
          </div>
          <div>
            <h3 className="mb-1.5 font-semibold">行业用的是申万</h3>
            <p className="mb-2 text-muted-foreground">A 股的行业都是申万（申万宏源证券编的行业分类，2021 版，券商研报和行业指数最常用）：一级 31 个、二级 131 个，带每只股票进出行业的日期。这一页、选股页、K 线页、资料，还有规则的股票池“只看行业”、IN_INDUSTRY、INDUSTRY_RANK，用的都是它，按当天的归属算。二级名字后面申万加的“Ⅱ”不显示。股票列表自带的 Tushare 行业（111 个、只有一层、没有历史）不用：它和申万对不上，规则写它选不出股票。日股是东证 33 業種。</p>
            <table className="w-full text-left text-[0.75rem]">
              <thead className="text-muted-foreground"><tr><th className="pr-3 font-normal">股票</th><th className="pr-3 font-normal">Tushare 的“行业”</th><th className="font-normal">申万一级 · 二级</th></tr></thead>
              <tbody>{SHENWAN.map((r) => <tr key={r[0]}>{r.map((c, i) => <td key={i} className="py-0.5 pr-3">{c}</td>)}</tr>)}</tbody>
            </table>
          </div>
          <div>
            <h3 className="mb-1.5 font-semibold">数从哪来、多久一次</h3>
            <p className="text-muted-foreground">A 股正式档配了 TickFlow 时，开盘期间每分钟取一次全市场行情（约 5,500 只，比实际晚 2–4 秒），这一页开着时每 10 秒一次；当天的涨跌停价开盘前从 Tushare 取。阶段和收盘后的数来自日线。9:15–9:25 集合竞价的参考价会变，不算；9:25 以后显示竞价结果。盘面变化只记在这一页，不推送。</p>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
