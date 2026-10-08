import { useEffect, useState } from "react";
import { BrainCircuit, ShieldCheck } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import type { ClassificationStatus } from "@/lib/types";

export function ClassificationModelCard() {
  const [status, setStatus] = useState<ClassificationStatus | null>(null);

  useEffect(() => {
    void api.classificationStatus().then(setStatus).catch(() => setStatus(null));
  }, []);

  if (!status) return null;
  const active = status.loaded && status.mode === "ml";

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <BrainCircuit className="h-4 w-4" />
          Classification model
          <span className={`ml-auto rounded-full px-2 py-0.5 text-xs font-semibold ${
            active ? "bg-emerald-100 text-emerald-800" : "bg-amber-100 text-amber-900"
          }`}>
            {active ? "ML active" : "Rule fallback"}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-xs">
        <p className="font-medium">{status.version}</p>
        {active ? (
          <p className="flex items-start gap-2 text-muted-foreground">
            <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-700" />
            <span>
              Trained on {status.training_rows.toLocaleString()} rows, including{" "}
              {status.gold_rows.toLocaleString()} reviewed examples. Human labels are protected.
            </span>
          </p>
        ) : (
          <p className="text-amber-900">
            The dashboard is still operating with the existing keyword rules.
            {status.error ? ` ${status.error}` : ""}
          </p>
        )}
        {status.unresolved_rows_excluded > 0 && (
          <p className="text-muted-foreground">
            {status.unresolved_rows_excluded} unresolved review row
            {status.unresolved_rows_excluded === 1 ? " was" : "s were"} excluded from training.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
