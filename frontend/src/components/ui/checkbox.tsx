interface Props {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  count?: number;
}

export function Checkbox({ checked, onChange, label, count }: Props) {
  return (
    <label className="flex cursor-pointer items-center gap-2 rounded-md px-1.5 py-1 text-sm hover:bg-muted">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4 rounded accent-[hsl(var(--primary))]"
      />
      <span className="flex-1 truncate">{label}</span>
      {count != null && <span className="text-xs text-muted-foreground">{count}</span>}
    </label>
  );
}
