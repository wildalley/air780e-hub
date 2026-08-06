import { Box, Stack, Typography } from '@mui/material'
import type { ReactNode } from 'react'

/**
 * Page header — one consistent title block across every page.
 *
 * Display scale comes from the theme's h1 (tight leading, negative tracking).
 * Actions sit on the same baseline as the title so the scan path is one row.
 */

interface Props {
  title: string
  subtitle?: string
  actions?: ReactNode
}

export function PageHeader({ title, subtitle, actions }: Props) {
  return (
    <Stack
      direction={{ xs: 'column', sm: 'row' }}
      spacing={1.5}
      sx={{
        alignItems: { sm: 'center' },
        minHeight: 44
      }}>
      <Box sx={{ flexGrow: 1, minWidth: 0 }}>
        <Typography variant="h1">{title}</Typography>
        {subtitle && (
          <Typography
            variant="body2"
            sx={{
              color: 'text.secondary',
              mt: 0.25
            }}>
            {subtitle}
          </Typography>
        )}
      </Box>
      {actions && (
        <Stack
          direction="row"
          spacing={1}
          sx={{
            alignItems: 'center',
            flexWrap: 'wrap'
          }}>
          {actions}
        </Stack>
      )}
    </Stack>
  )
}
