<?php

use App\Http\Controllers\JobController;
use Illuminate\Support\Facades\Route;

Route::get('/jobs/{id}', [JobController::class, 'getJob']);
