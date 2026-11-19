import { useMutation } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import type { User } from "../api/types";
import { Disclaimer } from "../components/Disclaimer";

export function Login({ onSignedIn }: { onSignedIn: (user: User) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const login = useMutation({
    mutationFn: () => api.login({ username, password }),
    onSuccess: onSignedIn,
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    login.mutate();
  };

  return (
    <div className="flex min-h-screen flex-col">
      <Disclaimer />
      <main className="grid flex-1 place-items-center px-4">
        <form onSubmit={submit} className="card w-full max-w-sm p-6 shadow-sm">
          <h1 className="text-lg font-semibold">The Support Desk</h1>
          <p className="mt-1 text-sm text-slate-500">
            Internal agent console. Sign in to continue.
          </p>
          <label className="mt-6 block text-sm font-medium" htmlFor="username">
            Username
          </label>
          <input
            id="username"
            className="input mt-1"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
            autoFocus
          />
          <label className="mt-4 block text-sm font-medium" htmlFor="password">
            Password
          </label>
          <input
            id="password"
            type="password"
            className="input mt-1"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          {login.isError && (
            <p role="alert" className="mt-3 text-sm text-red-700">
              {login.error.message}
            </p>
          )}
          <button type="submit" className="btn-primary mt-6 w-full py-2" disabled={login.isPending}>
            {login.isPending ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </main>
    </div>
  );
}
