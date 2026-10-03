"use client";

import type { Preview } from "@/lib/types";
import { Card, SectionTitle } from "./ui";

export function DataPreviewTable({ preview }: { preview: Preview }) {
  return (
    <div className="overflow-x-auto rounded-2xl border border-hairline">
      <table className="w-full border-collapse bg-white text-xs">
        <thead>
          <tr>
            {preview.columns.map((c) => (
              <th
                key={c}
                className="whitespace-nowrap border-b border-hairline bg-canvas/60 px-4 py-2.5 text-left font-medium text-muted"
              >
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {preview.rows.map((row, i) => (
            <tr key={i} className="transition-colors odd:bg-white even:bg-canvas/30">
              {preview.columns.map((c) => {
                const empty = row[c] === null || row[c] === "";
                return (
                  <td
                    key={c}
                    className="whitespace-nowrap px-4 py-2 text-ink/80"
                  >
                    {empty ? (
                      <span className="text-faint">—</span>
                    ) : (
                      String(row[c])
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function DataPreviewCard({ preview }: { preview: Preview }) {
  return (
    <Card>
      <SectionTitle
        title="数据预览"
        desc="解析后的前 10 行原始数据，请确认字段与内容是否符合预期。"
      />
      <DataPreviewTable preview={preview} />
    </Card>
  );
}
