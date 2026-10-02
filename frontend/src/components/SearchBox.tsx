interface SearchBoxProps {
  value: string
  onChange: (value: string) => void
  placeholder?: string
}

export function SearchBox({ value, onChange, placeholder = 'Filter this page…' }: SearchBoxProps) {
  return (
    <input
      type="search"
      className="search-box"
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
      aria-label="Filter"
    />
  )
}
