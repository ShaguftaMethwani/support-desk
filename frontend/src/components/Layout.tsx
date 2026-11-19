import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet } from "react-router";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import { Disclaimer } from "./Disclaimer";

const NAV = [
  { to: "/chat", label: "Chat" },
  { to: "/queue", label: "Queue" },
  { to: "/approvals", label: "Approvals", badge: "approvals" },
  { to: "/weekly", label: "Weekly pulse" },
  { to: "/explainers", label: "Explainers" },
  { to: "/sources", label: "Sources" },
] as const;

export function Layout() {
  const { user, signOut } = useAuth();
  const pending = useQuery({
    queryKey: ["actions"],
    queryFn: api.pendingActions,
    refetchInterval: 30_000,
  });
  const waiting = pending.data?.actions.filter((a) => a.status === "pending").length ?? 0;

  return (
    <div className="flex min-h-screen flex-col">
      <Disclaimer />
      <div className="flex flex-1 flex-col md:flex-row">
        <nav
          aria-label="Main"
          className="flex shrink-0 flex-col border-b border-slate-200 bg-white md:w-52 md:border-r md:border-b-0"
        >
          <div className="flex items-center justify-between px-4 py-3 md:block">
            <p className="text-sm font-semibold tracking-tight">The Support Desk</p>
            <p className="hidden text-xs text-slate-500 md:block">INDMoney · HDFC MF facts</p>
          </div>
          <ul className="flex gap-1 overflow-x-auto px-2 pb-2 md:flex-col md:overflow-visible md:pb-0">
            {NAV.map((n) => (
              <li key={n.to}>
                <NavLink
                  to={n.to}
                  className={({ isActive }) =>
                    `flex items-center justify-between gap-2 whitespace-nowrap rounded-md px-3 py-2 text-sm ${
                      isActive
                        ? "bg-accent-50 font-medium text-accent-800"
                        : "text-slate-700 hover:bg-slate-100"
                    }`
                  }
                >
                  {n.label}
                  {"badge" in n && waiting > 0 && (
                    <span className="rounded-full bg-accent-700 px-1.5 text-xs font-medium text-white">
                      {waiting}
                    </span>
                  )}
                </NavLink>
              </li>
            ))}
          </ul>
          <div className="mt-auto hidden border-t border-slate-200 px-4 py-3 text-xs text-slate-500 md:block">
            <p>
              Signed in as <span className="font-medium text-slate-700">{user.username}</span>
            </p>
            <button className="mt-1 text-accent-700 hover:underline" onClick={signOut}>
              Sign out
            </button>
          </div>
        </nav>
        <main className="min-w-0 flex-1 px-4 py-5 md:px-8 md:py-6">
          <Outlet />
        </main>
      </div>
      <div className="border-t border-slate-200 bg-white px-4 py-2 text-xs text-slate-500 md:hidden">
        {user.username} ·{" "}
        <button className="text-accent-700" onClick={signOut}>
          Sign out
        </button>
      </div>
    </div>
  );
}
