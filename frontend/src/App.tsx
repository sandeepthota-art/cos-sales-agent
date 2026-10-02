import { Navigate, Route, Routes } from 'react-router-dom'
import { RequireAuth } from './auth/RequireAuth'
import { AppShell } from './layouts/AppShell'
import { CalendarApprovalsPage } from './pages/CalendarApprovalsPage'
import { CommitmentsPage } from './pages/CommitmentsPage'
import { EmailsPage } from './pages/EmailsPage'
import { FollowUpsPage } from './pages/FollowUpsPage'
import { KnowledgePage } from './pages/KnowledgePage'
import { LoginPage } from './pages/LoginPage'
import { MeetingBriefPage } from './pages/MeetingBriefPage'
import { MeetingsPage } from './pages/MeetingsPage'
import { NotFoundPage } from './pages/NotFoundPage'
import { OpportunitiesPage } from './pages/OpportunitiesPage'
import { OpportunityDetailPage } from './pages/OpportunityDetailPage'
import { OrganizationDetailPage } from './pages/OrganizationDetailPage'
import { OrganizationsPage } from './pages/OrganizationsPage'
import { OverviewPage } from './pages/OverviewPage'
import { PeoplePage } from './pages/PeoplePage'
import { PersonDetailPage } from './pages/PersonDetailPage'
import { PersonalItemsPage } from './pages/PersonalItemsPage'
import { ProjectDetailPage } from './pages/ProjectDetailPage'
import { ProjectsPage } from './pages/ProjectsPage'
import { ReplyApprovalsPage } from './pages/ReplyApprovalsPage'
import { ThreadDetailPage } from './pages/ThreadDetailPage'
import { ThreadsPage } from './pages/ThreadsPage'

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/"
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route index element={<OverviewPage />} />
        <Route path="emails" element={<EmailsPage />} />
        <Route path="threads" element={<ThreadsPage />} />
        <Route path="threads/:threadId" element={<ThreadDetailPage />} />
        <Route path="people" element={<PeoplePage />} />
        <Route path="people/:personId" element={<PersonDetailPage />} />
        <Route path="organizations" element={<OrganizationsPage />} />
        <Route path="organizations/:orgId" element={<OrganizationDetailPage />} />
        <Route path="projects" element={<ProjectsPage />} />
        <Route path="projects/:projectId" element={<ProjectDetailPage />} />
        <Route path="opportunities" element={<OpportunitiesPage />} />
        <Route path="opportunities/:opportunityId" element={<OpportunityDetailPage />} />
        <Route path="commitments" element={<CommitmentsPage />} />
        <Route path="follow-ups" element={<FollowUpsPage />} />
        <Route path="meetings" element={<MeetingsPage />} />
        <Route path="meetings/:meetingId/brief" element={<MeetingBriefPage />} />
        <Route path="personal-items" element={<PersonalItemsPage />} />
        <Route path="knowledge" element={<KnowledgePage />} />
        <Route path="approvals/replies" element={<ReplyApprovalsPage />} />
        <Route path="approvals/calendar" element={<CalendarApprovalsPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
