import { BrandLabel } from "./BrandLabel";
import { useEffect, useRef } from 'react';
import { VERTICALS } from '@/lib/types';
import './brand-filters.css';

const DEVSOL = VERTICALS.filter(v => v !== 'Social Business');
const PENDING_BRANDS: string[] = [];

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
    <BrandLabel name={label}/></label>;
}

export function BrandFilters({ selectedVerticals, selectedBrands, availableBrands,
  onVerticalChange, onBrandChange, onClear }: {
  selectedVerticals: string[];
  selectedBrands: string[];
  availableBrands: string[];
  onVerticalChange: (values: string[]) => void;
  onBrandChange: (values: string[]) => void;
  onClear: () => void;
}) {
  const selectedCount = selectedVerticals.length + selectedBrands.length;
  const otherBrands = availableBrands.filter(brand => brand !== 'CMS');
  useEffect(() => {
    if (selectedVerticals.includes('Social Business')) {
      onVerticalChange(selectedVerticals.filter(v => v !== 'Social Business'));
    }
  }, [selectedVerticals, onVerticalChange]);
  return <section className="ud-brand-tree" aria-label="Brands">
    <div className="flex items-center justify-between"><h3>Brands</h3>{selectedCount > 0 &&
      <button type="button" onClick={onClear} className="text-xs underline">Clear</button>}</div>
    <div className="ud-brand-children">
      <details open><summary><BrandLabel name="CMS"/></summary><div className="ud-brand-children">
        <Selection label="All CMS" values={['CMS']} selected={selectedBrands} onChange={onBrandChange} />
        <details open className="ud-brand-branch"><summary>Devsol</summary><div className="ud-brand-children">
          <Selection label="All Devsol" values={DEVSOL} selected={selectedVerticals} onChange={onVerticalChange} />
          {DEVSOL.map(v => <Selection key={v} label={v} values={[v]} selected={selectedVerticals} onChange={onVerticalChange} />)}
        </div></details>
      </div></details>
      <Selection label="Select all below" values={availableBrands} selected={selectedBrands} onChange={onBrandChange} />
      {otherBrands.map(brand => <Selection key={brand} label={brand} values={[brand]}
        selected={selectedBrands} onChange={onBrandChange} />)}
    </div>
    {PENDING_BRANDS.length > 0 && <p className="ud-brand-note">Keywords not yet supplied</p>}
    {PENDING_BRANDS.map(brand => <label key={brand} className="ud-brand-option ud-brand-pending">
      <input type="checkbox" disabled /><BrandLabel name={brand} /></label>)}
  </section>;
}
