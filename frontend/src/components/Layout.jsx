import { Outlet, NavLink, useLocation, useNavigate } from "react-router-dom";
import {
  LayoutDashboard, Upload, Archive as ArchiveIcon,
  History, LogOut, User, Building2, ClipboardList,
} from "lucide-react";
import clsx from "clsx";
import ChatWidget from "./ChatWidget";
import { useAuth } from "../context/useAuth";
import logo from "../assets/logo.png";

const ROLE_LABELS = {
  admin:                { label: "Admin",                cls: "bg-crimson/5 text-crimson border-crimson/20" },
  technical_evaluator:  { label: "Technical",            cls: "bg-blue-50 text-blue-700 border-blue-200" },
  financial_evaluator:  { label: "Financial",            cls: "bg-violet-50 text-violet-700 border-violet-200" },
  operations_evaluator: { label: "Operations",           cls: "bg-orange-50 text-orange-700 border-orange-200" },
  legal_compliance:     { label: "Legal",                cls: "bg-purple-50 text-purple-700 border-purple-200" },
  business_rep:         { label: "Business Rep",         cls: "bg-teal-50 text-teal-700 border-teal-200" },
  ai_auditor:           { label: "AI Auditor",           cls: "bg-gray-100 text-gray-700 border-gray-200" },
};

// Nav items vary by role
function buildNav(role) {
  const evaluatorRoles = [
    "technical_evaluator","financial_evaluator",
    "operations_evaluator","legal_compliance",
  ];
  if (evaluatorRoles.includes(role)) {
    return [
      { to: "/my-evaluations", icon: ClipboardList,   label: "My Evaluations" },
      { to: "/archive",        icon: ArchiveIcon,      label: "Archive" },
    ];
  }
  if (role === "business_rep") {
    return [
      { to: "/dashboard", icon: LayoutDashboard, label: "Dashboard" },
      { to: "/archive",   icon: ArchiveIcon,     label: "Archive" },
    ];
  }
  if (role === "ai_auditor") {
    return [
      { to: "/tenant",    icon: Building2,       label: "Overview" },
      { to: "/dashboard", icon: LayoutDashboard, label: "RFPs" },
      { to: "/audit-log", icon: History,         label: "Audit Log" },
    ];
  }
  // Admin (default)
  return [
    { to: "/tenant",    icon: Building2,       label: "Overview" },
    { to: "/dashboard", icon: LayoutDashboard, label: "RFPs" },
    { to: "/archive",   icon: ArchiveIcon,     label: "Archive" },
    { to: "/upload",    icon: Upload,          label: "New RFP" },
    { to: "/audit-log", icon: History,         label: "Audit Log" },
  ];
}

export default function Layout() {
  const location = useLocation();
  const navigate = useNavigate();
  const { user, logout } = useAuth();

  const nav      = buildNav(user?.role);
  const roleInfo = ROLE_LABELS[user?.role] || { label: user?.role, cls: "bg-gray-100 text-gray-600" };

  // Extract rfpId from current path for ChatWidget
  const rfpIdMatch = location.pathname.match(/\/(?:evaluation|score-editor)\/([^/]+)/)
    || location.pathname.match(/\/evaluation\/([^/]+)\/compare/);
  const rfpId = rfpIdMatch ? rfpIdMatch[1] : null;

  // Only show chat for admin and evaluators (not business reps)
  const showChat = user?.role !== "business_rep";

  async function handleLogout() {
    await logout();
    navigate("/login", { replace: true });
  }

  return (
    <div className="flex h-screen bg-white">
      {/* Sidebar */}
      <aside className="w-56 flex-shrink-0 flex flex-col bg-white border-r border-gray-200 shadow-md">
        {/* Logo */}
        <div className="px-6 pt-7 pb-6 border-b border-gray-200">
          <div className="flex items-center gap-3">
            <img src={logo} alt="Logo" className="w-9 h-9 object-contain" />
            <div>
              <div className="font-display text-xl leading-none tracking-wide text-gray-900">RFP</div>
              <div className="font-body text-[10px] text-gray-400 tracking-widest uppercase">Evaluator</div>
            </div>
          </div>
        </div>

        {/* Nav */}
        <nav className="flex-1 px-3 py-5 space-y-1">
          {nav.map(({ to, icon: Icon, label }) => (
            <NavLink key={to} to={to}
              className={({ isActive }) => clsx(
                "flex items-center gap-3 px-3 py-2.5 rounded-lg font-body text-sm transition-all duration-150",
                isActive
                  ? "bg-crimson text-white"
                  : "text-gray-600 hover:text-gray-900 hover:bg-gray-100",
              )}>
              <Icon size={16} />
              {label}
            </NavLink>
          ))}
        </nav>

        {/* User info + logout */}
        <div className="px-4 py-4 border-t border-gray-200 space-y-3">
          {user && (
            <div className="flex items-center gap-2.5">
              <div className="w-8 h-8 rounded-full bg-crimson flex items-center justify-center flex-shrink-0">
                <User size={14} className="text-white" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-xs font-medium text-gray-900 truncate">
                  {user.display_name || user.email}
                </div>
                <span className={clsx(
                  "inline-block text-[9px] font-mono border px-1.5 py-0.5 rounded-full mt-0.5 leading-none",
                  roleInfo.cls,
                )}>
                  {roleInfo.label}
                </span>
              </div>
            </div>
          )}
          <button onClick={handleLogout}
            className="w-full flex items-center gap-2 px-3 py-2 text-xs text-gray-500 hover:text-crimson hover:bg-gray-50 rounded-lg transition-colors font-body">
            <LogOut size={13} /> Sign out
          </button>
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-y-auto relative">
        <Outlet />
        {showChat && <ChatWidget rfpId={rfpId} />}
      </main>
    </div>
  );
}
