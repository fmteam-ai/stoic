import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import "@/App.css";
import { AuthProvider } from "@/context/AuthContext";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { ErrorBoundary } from "@/components/ErrorBoundary";

import Login from "@/pages/Login";
import Register from "@/pages/Register";
import Dashboard from "@/pages/Dashboard";
import Signals from "@/pages/Signals";
import BotConfig from "@/pages/BotConfig";
import Accounts from "@/pages/Accounts";
import Trades from "@/pages/Trades";
import Symbols from "@/pages/Symbols";
import RiskCommander from "@/pages/RiskCommander";
import Subscription from "@/pages/Subscription";
import SubscriptionSuccess from "@/pages/SubscriptionSuccess";
import BrandingGallery from "@/pages/BrandingGallery";
import Affiliate from "@/pages/Affiliate";
import Notifications from "@/pages/Notifications";
import Analytics from "@/pages/Analytics";
import Settings from "@/pages/Settings";
import Billing from "@/pages/Billing";
import FAQ from "@/pages/FAQ";
import Guide from "@/pages/Guide";
import SafetyBlocks from "@/pages/SafetyBlocks";
import Agents from "@/pages/Agents";
import Strategies from "@/pages/Strategies";
import Portfolio from "@/pages/Portfolio";
import Execution from "@/pages/Execution";
import Research from "@/pages/Research";
import Crypto from "@/pages/Crypto";
import ShadowPerformance from "@/pages/ShadowPerformance";
import Scoreboard from "@/pages/Scoreboard";
import BrokerComparison from "@/pages/BrokerComparison";
import Scalp from "@/pages/Scalp";
import LossLab from "@/pages/LossLab";
import BotHealth from "@/pages/BotHealth";
import AffiliateLanding from "@/pages/AffiliateLanding";
import WelcomeTrailer from "@/pages/WelcomeTrailer";
import Terms from "@/pages/Terms";
import AdminUsers from "@/pages/AdminUsers";
import AdminAffiliates from "@/pages/AdminAffiliates";
import AdminMigration from "@/pages/AdminMigration";
import VerifyEmail from "@/pages/VerifyEmail";
import ForgotPassword from "@/pages/ForgotPassword";
import ResetPassword from "@/pages/ResetPassword";


function App() {
    return (
        <div className="App">
            <ErrorBoundary>
            <BrowserRouter>
                <AuthProvider>
                    <Routes>
                        <Route path="/login" element={<Login />} />
                        <Route path="/register" element={<Register />} />
                        {/* Public marketing trailer — no auth required */}
                        <Route path="/welcome" element={<WelcomeTrailer />} />
                        {/* Public marketing TOS — must be reachable pre-login */}
                        <Route path="/terms" element={<Terms />} />
                        {/* Public email-verification landing page */}
                        <Route path="/verify-email" element={<VerifyEmail />} />
                        {/* Public password-reset flow */}
                        <Route path="/forgot-password" element={<ForgotPassword />} />
                        <Route path="/reset-password" element={<ResetPassword />} />
                        {/* Public affiliate marketing page — no auth required */}
                        <Route path="/affiliates" element={<AffiliateLanding />} />
                        {/* Admin-only moderation console */}
                        <Route path="/admin/users" element={<ProtectedRoute requireAdmin><AdminUsers /></ProtectedRoute>} />
                        <Route path="/admin/affiliates" element={<ProtectedRoute requireAdmin><AdminAffiliates /></ProtectedRoute>} />
                        <Route path="/admin/migration" element={<ProtectedRoute requireAdmin><AdminMigration /></ProtectedRoute>} />
                        <Route path="/" element={<ProtectedRoute><Dashboard /></ProtectedRoute>} />
                        <Route path="/signals" element={<ProtectedRoute><Signals /></ProtectedRoute>} />
                        <Route path="/bot" element={<ProtectedRoute><BotConfig /></ProtectedRoute>} />
                        {/* Alias — `bot-config` is the documented path, while
                            the historical sidebar link is `/bot`. Both render
                            the same page so direct navigation works either way. */}
                        <Route path="/bot-config" element={<ProtectedRoute><BotConfig /></ProtectedRoute>} />
                        <Route path="/accounts" element={<ProtectedRoute><Accounts /></ProtectedRoute>} />
                        <Route path="/crypto" element={<ProtectedRoute><Crypto /></ProtectedRoute>} />
                        <Route path="/shadow-performance" element={<ProtectedRoute><ShadowPerformance /></ProtectedRoute>} />
                        <Route path="/scoreboard" element={<ProtectedRoute><Scoreboard /></ProtectedRoute>} />
                        <Route path="/brokers" element={<ProtectedRoute><BrokerComparison /></ProtectedRoute>} />
                        <Route path="/scalp" element={<ProtectedRoute><Scalp /></ProtectedRoute>} />
                        <Route path="/trades" element={<ProtectedRoute><Trades /></ProtectedRoute>} />
                        <Route path="/loss-lab" element={<ProtectedRoute><LossLab /></ProtectedRoute>} />
                        <Route path="/bot-health" element={<ProtectedRoute><BotHealth /></ProtectedRoute>} />
                        <Route path="/safety-blocks" element={<ProtectedRoute><SafetyBlocks /></ProtectedRoute>} />
                        <Route path="/symbols" element={<ProtectedRoute><Symbols /></ProtectedRoute>} />
                        <Route path="/commander" element={<ProtectedRoute><RiskCommander /></ProtectedRoute>} />
                        <Route path="/subscription" element={<ProtectedRoute><Subscription /></ProtectedRoute>} />
                        <Route path="/subscription/success" element={<ProtectedRoute><SubscriptionSuccess /></ProtectedRoute>} />
                        <Route path="/branding" element={<ProtectedRoute><BrandingGallery /></ProtectedRoute>} />
                        <Route path="/affiliate" element={<ProtectedRoute><Affiliate /></ProtectedRoute>} />
                        <Route path="/notifications" element={<ProtectedRoute><Notifications /></ProtectedRoute>} />
                        <Route path="/analytics" element={<ProtectedRoute><Analytics /></ProtectedRoute>} />
                        <Route path="/settings" element={<ProtectedRoute><Settings /></ProtectedRoute>} />
                        <Route path="/billing" element={<ProtectedRoute><Billing /></ProtectedRoute>} />
                        <Route path="/faq" element={<ProtectedRoute><FAQ /></ProtectedRoute>} />
                        <Route path="/guide" element={<ProtectedRoute><Guide /></ProtectedRoute>} />
                        <Route path="/agents" element={<ProtectedRoute><Agents /></ProtectedRoute>} />
                        <Route path="/strategies" element={<ProtectedRoute><Strategies /></ProtectedRoute>} />
                        <Route path="/portfolio" element={<ProtectedRoute><Portfolio /></ProtectedRoute>} />
                        <Route path="/execution" element={<ProtectedRoute><Execution /></ProtectedRoute>} />
                        <Route path="/research" element={<ProtectedRoute><Research /></ProtectedRoute>} />
                    </Routes>
                    <Toaster theme="dark" position="top-right" />
                </AuthProvider>
            </BrowserRouter>
            </ErrorBoundary>
        </div>
    );
}

export default App;
