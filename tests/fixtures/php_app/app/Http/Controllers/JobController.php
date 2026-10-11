<?php

namespace App\Http\Controllers;

use App\Repositories\JobRepository;

class JobController
{
    private JobRepository $repo;

    public function __construct(JobRepository $repo)
    {
        $this->repo = $repo;
    }

    public function getJob(int $id)
    {
        $this->repo->get($id);
        $this->repo->writeAudit($id);
    }
}
