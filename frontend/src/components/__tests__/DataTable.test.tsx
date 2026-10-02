import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { DataTable } from '../DataTable'

interface Row {
  id: string
  name: string
}

describe('DataTable', () => {
  it('renders a loading skeleton while loading', () => {
    render(<DataTable<Row> columns={[{ key: 'name', label: 'Name' }]} rows={[]} rowKey={(r) => r.id} loading />)
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  it('renders an empty state when there are no rows', () => {
    render(
      <DataTable<Row>
        columns={[{ key: 'name', label: 'Name' }]}
        rows={[]}
        rowKey={(r) => r.id}
        emptyTitle="Nothing to show"
      />,
    )
    expect(screen.getByText('Nothing to show')).toBeInTheDocument()
  })

  it('renders a row per item and invokes onRowClick', () => {
    const onRowClick = vi.fn()
    const rows: Row[] = [
      { id: '1', name: 'Alpha' },
      { id: '2', name: 'Beta' },
    ]
    render(
      <DataTable<Row>
        columns={[{ key: 'name', label: 'Name' }]}
        rows={rows}
        rowKey={(r) => r.id}
        onRowClick={onRowClick}
      />,
    )

    expect(screen.getByText('Alpha')).toBeInTheDocument()
    expect(screen.getByText('Beta')).toBeInTheDocument()

    fireEvent.click(screen.getByText('Beta'))
    expect(onRowClick).toHaveBeenCalledWith(rows[1])
  })
})
