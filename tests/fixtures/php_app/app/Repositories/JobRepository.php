<?php

namespace App\Repositories;

use App\Models\AuditLog;
use App\Models\Job;
use Illuminate\Support\Facades\DB;

class JobRepository
{
    public function get(int $id)
    {
        return Job::find($id);
    }

    public function writeAudit(int $id)
    {
        AuditLog::create(['job_id' => $id]);
        DB::table('audit_trail')->insert(['job_id' => $id]);
    }
}
