import { useEffect, useRef } from 'react';
import { VERTICALS } from '@/lib/types';
import './brand-filters.css';

const DEVSOL = VERTICALS.filter(v => v !== 'Social Business');
const OTHER_BRANDS = ['Green Foundation', 'Vrutti', 'Swasti', 'Setu', 'Upfront', 'Community Action Collab'];

export function brandPath(vertical: string) {
  return vertical === 'Social Business' ? 'CMS / Social Business' : `CMS / Devsol / ${vertical}`;
}

function Selection({ label, values, selected, onChange }: {
  label: string; values: readonly string[]; selected: string[]; onChange: (values: string[]) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const count = values.filter(v => selected.includes(v)).length;
  useEffect(() => { if (input.current) input.current.indeterminate = count > 0 && count < values.length; }, [count, values.length]);
  return <label className="ud-brand-option"><input ref={input} type="checkbox" checked={count === values.length}
    onChange={e => onChange(e.target.checked ? [...new Set([...selected, ...values])] : selected.filter(v => !values.includes(v)))} />
    <span>{label}</span></label>;
}

export function BrandFilters({ selected, onChange }: { selected: string[]; onChange: (values: string[]) => void }) {
  return <section className="ud-brand-tree" aria-label="Brands">
    <div className="flex items-center justify-between"><h3>Brands</h3>{selected.length > 0 &&
      <button type="button" onClick={() => onChange([])} className="text-xs underline">Clear</button>}</div>
    <details open><summary>CMS</summary><div className="ud-brand-children">
      <Selection label="All CMS" values={VERTICALS} selected={selected} onChange={onChange} />
      <details open className="ud-brand-branch"><summary>Devsol</summary><div className="ud-brand-children">
        <Selection label="All Devsol" values={DEVSOL} selected={selected} onChange={onChange} />
        {DEVSOL.map(v => <Selection key={v} label={v} values={[v]} selected={selected} onChange={onChange} />)}
      </div></details>
      <div className="ud-brand-branch ud-brand-social" role="group" aria-label="CMS / Social Business">
        <Selection label="Social Business" values={['Social Business']} selected={selected} onChange={onChange} />
      </div>
    </div></details>
    <p className="ud-brand-note">Other brands — opportunity assignments pending</p>
    {OTHER_BRANDS.map(brand => <label key={brand} className="ud-brand-option ud-brand-pending">
      <input type="checkbox" disabled /><span>{brand}</span></label>)}
  </section>;
}
