import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "@/index.css";
import App from "@/App";
import { installConsoleCapture } from "@/lib/diagnostics";

installConsoleCapture();

// Dev-only (stripped from production builds): the preview proxy hard-kills
// WebSockets after ~60s and Vite's only recovery is location.reload(),
// which made the preview refresh every minute. Vite awaits this listener
// before reloading — never resolving it keeps the page stable.
if (import.meta.hot) {
  import.meta.hot.on("vite:ws:disconnect", () => new Promise(() => {}));
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      refetchOnWindowFocus: false,
    },
  },
});

const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
