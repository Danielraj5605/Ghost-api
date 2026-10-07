import { useMutation, useQuery } from "@tanstack/react-query";
import { CheckCircle2, KeyRound, Stethoscope, XCircle } from "lucide-react";
import { api } from "../api";
import { Button, Card, ErrorBox, Pill, SectionTitle } from "../components/ui";

export function Settings() {
  const state = useQuery({ queryKey: ["state"], queryFn: api.state });
  const doctor = useMutation({ mutationFn: api.doctor });

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <h1 className="text-2xl font-bold">Settings</h1>

      <Card>
        <SectionTitle
          right={
            <Button onClick={() => doctor.mutate()} disabled={doctor.isPending}>
              <Stethoscope className="size-4" />
              {doctor.isPending ? "Checking…" : "Check providers"}
            </Button>
          }
        >
          LLM providers
        </SectionTitle>
        <div className="space-y-3 p-4 text-sm">
          <p className="text-muted">
            Sends one tiny request to each configured provider to check the key and model. This uses one LLM call
            per provider.
          </p>
          {doctor.error && <ErrorBox error={doctor.error} />}
          {doctor.data && (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-xs text-muted">
                  <tr>
                    <th className="py-2 pr-3 font-medium">Provider</th>
                    <th className="py-2 pr-3 font-medium">Model</th>
                    <th className="py-2 pr-3 font-medium">Status</th>
                    <th className="py-2 font-medium">Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {doctor.data.map((row) => (
                    <tr key={`${row.provider}-${row.model}`} className="border-t border-line align-top">
                      <td className="py-2 pr-3 font-medium">{row.provider}</td>
                      <td className="py-2 pr-3 font-mono text-xs">{row.model}</td>
                      <td className="py-2 pr-3">
                        {!row.key ? (
                          <Pill>no key</Pill>
                        ) : row.ok ? (
                          <Pill className="bg-ok/15 text-ok">
                            <CheckCircle2 className="size-3" /> OK · {row.seconds.toFixed(1)} s
                          </Pill>
                        ) : (
                          <Pill className="bg-bad/15 text-bad">
                            <XCircle className="size-3" /> failed
                          </Pill>
                        )}
                      </td>
                      <td className="py-2 text-xs text-muted">{row.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {state.data && !state.data.has_llm && (
            <p className="flex items-start gap-2 rounded-lg bg-warn/10 px-3 py-2 text-warn">
              <KeyRound className="mt-0.5 size-4 shrink-0" />
              No API key found. Add one (for example GEMINI_API_KEY or GROQ_API_KEY) to the .env file in the
              project folder, then restart <code className="font-mono">ghost-api ui</code>.
            </p>
          )}
        </div>
      </Card>

      <Card>
        <SectionTitle>Being a polite visitor</SectionTitle>
        <div className="space-y-2 p-4 text-sm text-muted">
          <p>
            Some sites (like Wikipedia) ask automated tools to say who runs them. Set{" "}
            <code className="font-mono text-fg">GHOST_API_CONTACT</code> in .env to your email or a URL and Ghost API
            adds it to its user agent. It is never filled in for you.
          </p>
          <p>Ghost API always follows robots.txt from the dashboard and stops at logins and bot checks.</p>
        </div>
      </Card>

      <Card>
        <SectionTitle>Where your data lives</SectionTitle>
        <dl className="grid gap-3 p-4 text-sm sm:grid-cols-[8rem_1fr]">
          <dt className="text-muted">Database</dt>
          <dd className="font-mono text-xs break-all">{state.data?.db ?? "…"}</dd>
          <dt className="text-muted">Output folder</dt>
          <dd className="font-mono text-xs break-all">{state.data?.out ?? "…"}</dd>
          <dt className="text-muted">Version</dt>
          <dd>{state.data?.version ?? "…"}</dd>
        </dl>
      </Card>
    </div>
  );
}
