import { Component, lazy, type ReactNode, Suspense, useState } from "react"
import { Navigate, Route, Routes, useLocation } from "react-router"
import { Asking } from "@/components/Ask"
import { HoverTips } from "@/components/HoverTips"
import { Button } from "@/components/ui/button"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import PhasePage from "@/pages/PhasePage"
import { CommandPalette } from "./CommandPalette"
import { MAIN_NAV, STRATEGY_NAV } from "./nav"
import { Sidebar } from "./Sidebar"
import { StatusBar } from "./StatusBar"
import { useAppearance } from "./theme"
import { useLiveFeed } from "@/lib/live"

const TodayPage = lazy(() => import("@/pages/TodayPage"))
const KLinePage = lazy(() => import("@/pages/KLinePage"))
const WatchPage = lazy(() => import("@/pages/WatchPage"))
const ValidatePage = lazy(() => import("@/pages/ValidatePage"))
const DataPage = lazy(() => import("@/pages/DataPage"))
const SettingsPage = lazy(() => import("@/pages/SettingsPage"))
const RulesPage = lazy(() => import("@/pages/RulesPage"))
const RuleWorkspacePage = lazy(() => import("@/pages/RuleWorkspacePage"))
const RuleDraftPage = lazy(() => import("@/pages/RuleDraftPage"))
const IndicatorsPage = lazy(() => import("@/pages/IndicatorsPage"))
const IndicatorEditPage = lazy(() => import("@/pages/IndicatorEditPage"))
const LabelsPage = lazy(() => import("@/pages/LabelsPage"))
const ScreenPage = lazy(() => import("@/pages/ScreenPage"))
const MarketPage = lazy(() => import("@/pages/MarketPage"))
const LivePage = lazy(() => import("@/pages/LivePage"))
const ResearchPage = lazy(() => import("@/pages/ResearchPage"))

/** A page that cannot load (its code gone: frontend/dist built again while the app is open) or throws while drawing: said
 * where the page was, the sidebar and the status bar kept — without it the whole window went blank. Going to another
 * page clears it; the pages themselves stay mounted as before (the K 线 page keeps its chart from stock to stock). */
class PageError extends Component<{ at: string; children: ReactNode }, { error: Error | null; at: string }> {
  state: { error: Error | null; at: string } = { error: null, at: this.props.at }
  static getDerivedStateFromError(error: Error) { return { error } }
  static getDerivedStateFromProps(p: { at: string }, s: { at: string }) { return p.at !== s.at ? { error: null, at: p.at } : null }
  render() {
    const e = this.state.error
    if (!e) return this.props.children
    const stale = /dynamically imported module|module script failed/i.test(e.message)
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center text-[0.875rem]" data-testid="page-error">
        <p>{stale ? "界面更新过了：这一页要重新载入" : `这一页出错了：${e.message}`}</p>
        <Button size="sm" variant="outline" onClick={() => window.location.reload()}>重新载入</Button>
      </div>
    )
  }
}

export function App() {
  const { dark } = useAppearance()
  const { pathname } = useLocation()
  const [paletteOpen, setPaletteOpen] = useState(false)
  useLiveFeed() // 盘中: the feed on every page, so an alert reaches you wherever you are (design 14)
  return (
    <TooltipProvider delayDuration={300}>
      <div className="flex h-screen w-screen overflow-hidden">
        <Sidebar onSearch={() => setPaletteOpen(true)} />
        <div className="flex min-w-0 flex-1 flex-col">
          <main className="min-h-0 flex-1 overflow-hidden">
            <PageError at={pathname}>
            <Suspense fallback={null}>
              <Routes>
                <Route path="/" element={<TodayPage />} />
                <Route path="/screen" element={<ScreenPage />} />
                <Route path="/market" element={<MarketPage />} />
                <Route path="/k" element={<KLinePage />} />
                <Route path="/k/:listingId" element={<KLinePage />} />
                <Route path="/live" element={<LivePage />} />
                <Route path="/watch" element={<WatchPage />} />
                <Route path="/research" element={<ResearchPage />} />
                <Route path="/data" element={<DataPage />} />
                <Route path="/settings" element={<SettingsPage />} />
                <Route path="/rules" element={<RulesPage />} />
                <Route path="/rules/draft" element={<RuleDraftPage />} />
                <Route path="/rules/:ruleId" element={<RuleWorkspacePage />} />
                <Route path="/indicators" element={<IndicatorsPage />} />
                <Route path="/indicators/:id" element={<IndicatorEditPage />} />
                <Route path="/labels" element={<LabelsPage />} />
                <Route path="/validate" element={<ValidatePage />} />
                {[...MAIN_NAV, ...STRATEGY_NAV].filter((n) => n.phase).map((n) => (
                  <Route key={n.to} path={n.to} element={<PhasePage item={n} />} />
                ))}
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
            </Suspense>
            </PageError>
          </main>
          <StatusBar />
        </div>
      </div>
      <CommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} />
      <Asking />
      <HoverTips />
      <Toaster theme={dark ? "dark" : "light"} position="bottom-right" />
    </TooltipProvider>
  )
}
