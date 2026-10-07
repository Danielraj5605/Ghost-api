import { ShieldAlert } from "lucide-react";
import type { EndpointDetail, Exposure } from "../types";
import { cx, Pill } from "./ui";

// Same words as ghost_api/capture/personal.py LABEL.
const KIND_LABEL: Record<string, string> = {
  pan: "PAN number",
  gstin: "GST number",
  aadhaar: "Aadhaar number",
  bank_account: "bank account number",
  card: "card number",
  upi: "UPI ID",
  email: "email address",
  phone: "phone number",
  date_of_birth: "date of birth",
  account_holder: "account holder's name",
};

export const kindLabel = (kind: string) => KIND_LABEL[kind] ?? kind;

/** On an endpoint card: this endpoint returns personal data. */
export function ExposureBadge({ exposure }: { exposure: Exposure }) {
  return (
    <Pill
      className={exposure.severity === "high" ? "bg-bad/15 text-bad" : "bg-warn/15 text-warn"}
      title={`Returns ${exposure.kinds.join(", ")}${exposure.open ? " without a login" : ""}`}
    >
      <ShieldAlert className="size-3" /> Personal data
    </Pill>
  );
}

/** In an endpoint's details: what was found, masked, and what to do. */
export function ExposurePanel({ exposure }: { exposure: Exposure }) {
  const high = exposure.severity === "high";
  return (
    <section
      aria-label="Personal data exposed"
      className={cx(
        "rounded-xl border p-3 sm:p-4",
        high ? "border-bad/40 bg-bad/5" : "border-warn/40 bg-warn/5",
      )}
    >
      <h4 className={cx("flex items-center gap-2 text-sm font-semibold", high ? "text-bad" : "text-warn")}>
        <ShieldAlert className="size-4" aria-hidden />
        Personal data {exposure.open ? "exposed without a login" : "in this endpoint's replies"}
      </h4>
      <p className="mt-1 text-sm text-muted">
        Ghost API hid these values: they are not stored, shown or exported. Examples are masked.
      </p>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-muted">
            <tr>
              <th className="py-1.5 pr-3 font-medium">Field</th>
              <th className="py-1.5 pr-3 font-medium">What</th>
              <th className="py-1.5 pr-3 font-medium">Seen</th>
              <th className="py-1.5 font-medium">Example</th>
            </tr>
          </thead>
          <tbody>
            {exposure.fields.map((f) => (
              <tr key={`${f.path}-${f.kind}`} className="border-t border-line/70 align-top">
                <td className="py-1.5 pr-3 font-mono text-xs break-all">{f.path}</td>
                <td className="py-1.5 pr-3">
                  {kindLabel(f.kind)}
                  {f.severity === "high" && <span className="ml-1 text-xs text-bad">(sensitive)</span>}
                </td>
                <td className="py-1.5 pr-3 tabular-nums">{f.count}</td>
                <td className="py-1.5 font-mono text-xs text-muted">{f.example}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-3 text-xs text-muted">
        If this is your site, stop returning these fields or require a login. If it isn't, don't
        collect or share them, and consider telling the site owner privately.
      </p>
    </section>
  );
}

function sentence(n: number, withoutLogin: boolean): string {
  const subject = n === 1 ? "1 endpoint" : `${n} endpoints`;
  if (withoutLogin) return `${subject} ${n === 1 ? "exposes" : "expose"} personal data without a login`;
  return `${subject} ${n === 1 ? "returns" : "return"} personal data`;
}

/** At the top of a site page: how many endpoints expose personal data. */
export function ExposureBanner({ endpoints }: { endpoints: EndpointDetail[] }) {
  const exposed = endpoints.map((e) => e.exposure).filter((x): x is Exposure => !!x);
  if (!exposed.length) return null;
  const open = exposed.filter((x) => x.open);
  const kinds = [...new Set((open.length ? open : exposed).flatMap((x) => x.kinds))].sort();
  const high = exposed.some((x) => x.severity === "high");
  return (
    <div
      role="alert"
      className={cx(
        "flex items-start gap-3 rounded-xl border px-4 py-3 text-sm",
        high ? "border-bad/40 bg-bad/10" : "border-warn/40 bg-warn/10",
      )}
    >
      <ShieldAlert className={cx("mt-0.5 size-5 shrink-0", high ? "text-bad" : "text-warn")} aria-hidden />
      <div>
        <p className="font-semibold">
          {sentence(open.length || exposed.length, open.length > 0)}: {kinds.join(", ")}
        </p>
        <p className="mt-0.5 text-muted">
          Ghost API hid these values. Open an endpoint marked "Personal data" to see which fields.
        </p>
      </div>
    </div>
  );
}
