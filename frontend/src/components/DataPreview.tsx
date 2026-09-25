"use client";

import type { Preview } from "@/lib/types";
import { Card, SectionTitle } from "./ui";

export function DataPreviewTable({ preview }: { preview: Preview }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr>
            {preview.columns.map((c) => (
              <th
                key={c}
                className="whitespace-nowrap border-b border-zinc-200 px-3 py-2 text-left font-medium text-zinc-500"
              >
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {preview.rows.map((row, i) => (
            <tr key={i} className="even:bg-zinc-50">
              {preview.columns.map((c) => (
                <td key={c} className="whitespace-nowrap px-3 py-1.5 text-zinc-700">
                  {row[c] === null || row[c] === "" ? (
                    <span className="text-amber-600">（空）</span>
                  ) : (
                    String(row[c])
                  )}
                </td>
              ))}
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
      <SectionTitle title="数据预览" desc="解析后的前 10 行原始数据" />
      <DataPreviewTable preview={preview} />
    </Card>
  );
}
