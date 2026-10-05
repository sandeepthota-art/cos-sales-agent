import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { AuthProvider } from '../../auth/AuthContext'
import { installMockFetch } from '../../test-utils/mockFetch'
import { OrganizationDetailPage } from '../OrganizationDetailPage'

const ORG_SUMMARY_BODY = {
  organization: {
    id: 'ORG-001',
    name: 'Databeat',
    domain: 'databeat.io',
    industry: 'AdTech / Programmatic Advertising Analytics',
    description: 'Advanced analytics and programmatic advertising optimization for AdTech partners.',
    products_services: ['Programmatic ad-ops analytics', 'Data engineering'],
    size_estimate: '220 employees',
    headquarters: 'Princeton, New Jersey, USA',
    website: 'https://databeat.io',
  },
  summary: {
    people: [],
    projects: [],
    related_commitments: [],
    related_follow_ups: [],
    related_meetings: [],
    related_knowledge_items: [],
    relationship_notes: {},
  },
}

describe('OrganizationDetailPage', () => {
  it('renders the researched company profile', async () => {
    installMockFetch((url) => {
      if (url.includes('/organizations/ORG-001')) return { status: 200, body: ORG_SUMMARY_BODY }
      return { status: 404 }
    })

    render(
      <MemoryRouter initialEntries={['/organizations/ORG-001']}>
        <AuthProvider>
          <Routes>
            <Route path="/organizations/:orgId" element={<OrganizationDetailPage />} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    )

    expect(await screen.findByRole('heading', { name: 'Databeat' })).toBeInTheDocument()
    expect(screen.getByText('AdTech / Programmatic Advertising Analytics')).toBeInTheDocument()
    expect(screen.getByText(/Advanced analytics and programmatic advertising optimization/)).toBeInTheDocument()
    expect(screen.getByText(/Princeton, New Jersey/)).toBeInTheDocument()
    expect(screen.getByText(/Programmatic ad-ops analytics/)).toBeInTheDocument()
  })

  it('renders a placeholder when no research has been done yet', async () => {
    installMockFetch((url) => {
      if (url.includes('/organizations/ORG-002')) {
        return {
          status: 200,
          body: {
            organization: { id: 'ORG-002', name: 'New Co', domain: 'newco.com' },
            summary: ORG_SUMMARY_BODY.summary,
          },
        }
      }
      return { status: 404 }
    })

    render(
      <MemoryRouter initialEntries={['/organizations/ORG-002']}>
        <AuthProvider>
          <Routes>
            <Route path="/organizations/:orgId" element={<OrganizationDetailPage />} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    )

    expect(await screen.findByRole('heading', { name: 'New Co' })).toBeInTheDocument()
    expect(screen.getByText(/No company research yet/)).toBeInTheDocument()
  })
})
