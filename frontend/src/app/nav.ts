// Navigation (design 00 应用外框). Pages not built yet say which phase delivers them (spec §13).
import {
  BarChart3, Bot, Database, FlaskConical, Gauge, Inbox, Library, LineChart, ListFilter, Settings, Activity, Eye, SquareDashed,
} from "lucide-react"

export interface NavItem { to: string; label: string; icon: typeof Inbox; phase?: string; summary?: string }

export const MAIN_NAV: NavItem[] = [
  { to: "/", label: "今日", icon: Inbox },
  { to: "/market", label: "市场", icon: Gauge },
  { to: "/screen", label: "选股", icon: ListFilter },
  { to: "/k", label: "K 线", icon: BarChart3 },
  { to: "/live", label: "盯盘", icon: Activity },
  { to: "/watch", label: "观察", icon: Eye },
  { to: "/research", label: "研究", icon: Bot },
]

export const STRATEGY_NAV: NavItem[] = [
  { to: "/rules", label: "规则库", icon: Library },
  { to: "/indicators", label: "指标库", icon: LineChart },
  { to: "/labels", label: "箱体标注", icon: SquareDashed },
  { to: "/validate", label: "验证", icon: FlaskConical },
]

export const BOTTOM_NAV: NavItem[] = [
  { to: "/data", label: "数据", icon: Database },
  { to: "/settings", label: "设置", icon: Settings },
]

export const ALL_NAV = [...MAIN_NAV, ...STRATEGY_NAV, ...BOTTOM_NAV]
