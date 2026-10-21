import "./index.css"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { HashRouter } from "react-router"
import { App } from "./app/App"
import { desktop } from "./app/platform"
import { applyKept } from "./app/theme"

// Windows: the rows under the window's own buttons leave them room (index.css .clear-window-buttons)
if (desktop.isWindows) document.documentElement.classList.add("win-overlay")
applyKept()   // the theme, font and size of the last start, before the first paint

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 30_000 } },
})

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <HashRouter>
        <App />
      </HashRouter>
    </QueryClientProvider>
  </StrictMode>,
)
