import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, type ReactNode } from "react";
import { api, ApiError, UNAUTHORIZED_EVENT } from "../api/client";
import type { User } from "../api/types";
import { Login } from "./Login";

interface AuthState {
  user: User;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}

/** Shows the login screen until /auth/me returns a user. Every view sits behind this. */
export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const me = useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      try {
        return await api.me();
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null;
        throw e;
      }
    },
    staleTime: Infinity,
    retry: false,
  });

  useEffect(() => {
    const onUnauthorized = () => {
      qc.setQueryData(["me"], null);
      qc.removeQueries({ predicate: (q) => q.queryKey[0] !== "me" });
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [qc]);

  if (me.isPending) {
    return <div className="grid h-screen place-items-center text-sm text-slate-500">Loading…</div>;
  }
  if (me.isError) {
    return (
      <div className="grid h-screen place-items-center p-4 text-center text-sm text-slate-600">
        <div>
          <p className="font-medium text-slate-900">Cannot reach the API.</p>
          <p className="mt-1">{me.error.message}</p>
          <button className="btn-secondary mt-4" onClick={() => me.refetch()}>
            Try again
          </button>
        </div>
      </div>
    );
  }
  if (!me.data) {
    return <Login onSignedIn={(user) => qc.setQueryData(["me"], user)} />;
  }

  const signOut = async () => {
    await api.logout().catch(() => undefined);
    qc.clear();
    qc.setQueryData(["me"], null);
  };
  return <AuthContext.Provider value={{ user: me.data, signOut }}>{children}</AuthContext.Provider>;
}
